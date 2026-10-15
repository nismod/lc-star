"""Download the Saint Lucia 2022 aerial photography as GeoTIFF tiles.

Requests are small, slow and sequential; successful existing tiles are
skipped on later runs.
"""

from __future__ import annotations

import argparse
import math
import os
import random
import sys
import time
from pathlib import Path

import requests
from osgeo import gdal, ogr, osr
from shapely.geometry import box, shape
from shapely.ops import transform, unary_union
from shapely.prepared import prep

URL = "https://geoservices.govt.lc/geoserver/wms"
LAYER = "SaintLucia:lidar_ortho_2022_10cm"
BUFFER_FILE = Path("stlucia-buffer.gpkg")
BOUNDS = (706_000, 733_000, 1_509_000, 1_567_000)  # xmin, xmax, ymin, ymax, EPSG:32620
TILE_METRES = 500
PIXELS_PER_METRE = 10  # 10 cm pixels, should be native (max) resolution


def tile_path(root: Path, x: int, y: int) -> Path:
    # Group by the lower 10 km coordinate bucket (e.g. x715000 -> x710000).
    return (
        root
        / f"x{x // 10_000 * 10_000}"
        / f"y{y // 10_000 * 10_000}"
        / f"ortho_{x}_{y}.tif"
    )


def is_valid_tile(path: Path, x: int, y: int, width: int, height: int) -> bool:
    if not path.is_file() or path.stat().st_size < 1024:
        return False
    ds = gdal.Open(str(path), gdal.GA_ReadOnly)
    if ds is None or ds.RasterXSize != width or ds.RasterYSize != height:
        return False
    gt = ds.GetGeoTransform()
    valid = (
        abs(gt[0] - x) < 0.01
        and abs(gt[3] - (y + TILE_METRES)) < 0.01
        and abs(gt[1] - 1 / PIXELS_PER_METRE) < 1e-9
        and abs(gt[5] + 1 / PIXELS_PER_METRE) < 1e-9
    )
    ds = None
    return valid


def write_lzw(source: Path, destination: Path) -> None:
    src = gdal.Open(str(source), gdal.GA_ReadOnly)
    if src is None:
        raise RuntimeError("response was not a readable GeoTIFF")
    result = gdal.Translate(
        str(destination),
        src,
        format="GTiff",
        creationOptions=["COMPRESS=LZW", "TILED=YES"],
    )
    src = None
    if result is None:
        raise RuntimeError("GDAL could not write the compressed GeoTIFF")
    result.FlushCache()
    result = None


def load_prepared_buffer(path: Path):
    """Read the buffer dataset once and prepare its union in EPSG:32620."""
    if not path.is_file():
        raise FileNotFoundError(f"buffer GeoPackage not found: {path}")
    dataset = ogr.Open(str(path), 0)
    if dataset is None:
        raise RuntimeError(f"GDAL/OGR could not open {path}")
    layer = dataset.GetLayer(0)
    if layer is None:
        raise ValueError(f"no vector layer found in {path}")
    source_crs = layer.GetSpatialRef()
    if source_crs is None:
        raise ValueError(f"{path} has no CRS; cannot compare it with EPSG:32620 tiles")
    target_crs = osr.SpatialReference()
    target_crs.ImportFromEPSG(32620)
    if not source_crs.IsSame(target_crs):
        transform_coords = osr.CoordinateTransformation(source_crs, target_crs)
        project = lambda x, y, z=None: transform_coords.TransformPoint(x, y)[:2]
    else:
        project = None
    geometries = []
    for feature in layer:
        ogr_geometry = feature.GetGeometryRef()
        if ogr_geometry:
            geom = shape(__import__("json").loads(ogr_geometry.ExportToJson()))
            if project:
                geom = transform(project, geom)
            if not geom.is_empty:
                geometries.append(geom)
    layer = None
    dataset = None
    if not geometries:
        raise ValueError(f"no usable geometries found in {path}")
    # Union once; prep() makes repeated tile-intersection checks cheap.
    buffer_geometry = unary_union(geometries)
    return prep(buffer_geometry)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, nargs="?", default=Path("ortho-tiles"))
    parser.add_argument(
        "--tile-metres",
        type=int,
        default=TILE_METRES,
        help="square tile width in map metres (default: 500, any bigger risks server errors)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=10.0,
        help="pause between requests (default: 10 seconds)",
    )
    parser.add_argument(
        "--retries", type=int, default=6, help="retries per tile (default: 6)"
    )
    parser.add_argument(
        "--max-backoff",
        type=float,
        default=120.0,
        help="maximum retry pause in seconds",
    )
    parser.add_argument(
        "--start-x",
        type=int,
        help="optional tile-aligned minimum easting for a partial run",
    )
    parser.add_argument(
        "--end-x", type=int, help="optional exclusive maximum easting for a partial run"
    )
    parser.add_argument(
        "--start-y",
        type=int,
        help="optional tile-aligned minimum northing for a partial run",
    )
    parser.add_argument(
        "--end-y",
        type=int,
        help="optional exclusive maximum northing for a partial run",
    )
    args = parser.parse_args()

    if args.tile_metres <= 0 or (args.tile_metres * PIXELS_PER_METRE) > 10_000:
        parser.error(
            "tile size must be positive and at most 1000 metres (10,000 pixels per side)"
        )
    if args.delay < 0 or args.retries < 0:
        parser.error("delay and retries must be non-negative")
    gdal.UseExceptions()
    buffer_geometry = load_prepared_buffer(BUFFER_FILE)
    print(f"Loaded and prepared buffer geometry from {BUFFER_FILE}", flush=True)

    xmin, xmax, ymin, ymax = BOUNDS
    step = args.tile_metres
    # Include final partial tiles on the east/north edges; the server returns the
    # requested pixel dimensions across each bbox, so clamp bbox to coverage.
    xs = range(
        args.start_x if args.start_x is not None else xmin,
        args.end_x if args.end_x is not None else xmax,
        step,
    )
    ys = range(
        args.start_y if args.start_y is not None else ymin,
        args.end_y if args.end_y is not None else ymax,
        step,
    )
    total = len(xs) * len(ys)
    done = skipped = failed = 0
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Saint Lucia orthophoto archive; sequential polite tile requests"
        }
    )

    for y in ys:
        for x in xs:
            x2, y2 = min(x + step, xmax), min(y + step, ymax)
            if not buffer_geometry.intersects(box(x, y, x2, y2)):
                skipped += 1
                done += 1
                continue
            width = math.ceil((x2 - x) * PIXELS_PER_METRE)
            height = math.ceil((y2 - y) * PIXELS_PER_METRE)
            dest = tile_path(args.output, x, y)
            if is_valid_tile(dest, x, y, width, height):
                skipped += 1
                done += 1
                continue

            dest.parent.mkdir(parents=True, exist_ok=True)
            params = {
                "service": "WMS",
                "version": "1.1.1",
                "request": "GetMap",
                "layers": LAYER,
                "styles": "",
                "srs": "EPSG:32620",
                "bbox": f"{x},{y},{x2},{y2}",
                "width": str(width),
                "height": str(height),
                "format": "image/geotiff",
            }
            tmp = dest.with_suffix(".download.tif")
            success = False
            for attempt in range(args.retries + 1):
                try:
                    # Stream directly to a temporary file; error XML is never
                    # mistaken for a raster and interrupted downloads are resumable.
                    with session.get(
                        URL, params=params, timeout=(30, 300), stream=True
                    ) as response:
                        response.raise_for_status()
                        content_type = response.headers.get("Content-Type", "").lower()
                        with tmp.open("wb") as stream:
                            for chunk in response.iter_content(1024 * 1024):
                                if chunk:
                                    stream.write(chunk)
                    if "xml" in content_type or not is_valid_tile(
                        tmp, x, y, width, height
                    ):
                        raise RuntimeError(
                            f"response is not the expected {width}x{height} GeoTIFF (Content-Type: {content_type})"
                        )
                    compressed = dest.with_suffix(".compressed.tif")
                    write_lzw(tmp, compressed)
                    os.replace(compressed, dest)
                    tmp.unlink(missing_ok=True)
                    success = True
                    break
                except Exception as exc:  # noqa
                    tmp.unlink(missing_ok=True)
                    if attempt >= args.retries:
                        print(f"FAILED x={x} y={y}: {exc}", file=sys.stderr, flush=True)
                        break
                    pause = min(
                        args.max_backoff, args.delay * (2**attempt)
                    ) + random.uniform(0, 1)
                    print(
                        f"Retry {attempt + 1}/{args.retries} x={x} y={y} in {pause:.1f}s: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                    time.sleep(pause)
            if success:
                print(f"Saved {dest}", flush=True)
            else:
                failed += 1
            done += 1
            if done < total:
                time.sleep(args.delay)

    print(f"Finished: {done}/{total} tiles; {skipped} already valid; {failed} failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
