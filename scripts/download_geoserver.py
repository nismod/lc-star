"""Inventory and archive public datasets from a GeoServer."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

BASE = os.environ.get("GEOSERVER_URL", "https://geoservices.govt.lc/geoserver").rstrip(
    "/"
)
UA = "GeoServer public data archive; sequential requests with delay"


def fetch(url: str, path: Path, delay: float, timeout: int) -> bool:
    """Fetch one URL, retry once, and return False for unavailable endpoints."""
    time.sleep(delay)
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(2):
        try:
            with (
                urllib.request.urlopen(request, timeout=timeout) as response,
                path.open("wb") as out,
            ):
                while chunk := response.read(1024 * 1024):
                    out.write(chunk)
            return True
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt:
                print(f"Unavailable: {url} ({exc})", file=sys.stderr)
            else:
                time.sleep(3)
    path.unlink(missing_ok=True)
    return False


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_capabilities(folder: Path) -> tuple[list[dict], dict[str, dict]]:
    records: list[dict] = []
    vectors: dict[str, dict] = {}
    for service in ("wms", "wfs", "wcs"):
        try:
            root = ET.parse(folder / f"{service}.xml").getroot()
        except ET.ParseError, OSError:
            continue
        if service == "wms":
            # WMS provides rendering; capture names/titles and advertised formats.
            formats = [
                e.text for e in root.iter() if local(e.tag) == "Format" and e.text
            ]
            for layer in root.iter():
                if local(layer.tag) != "Layer":
                    continue
                fields = {
                    local(e.tag): (e.text or "").strip()
                    for e in layer
                    if local(e.tag) in {"Name", "Title", "Abstract"}
                }
                if fields.get("Name"):
                    records.append({"service": service, **fields, "formats": formats})
        elif service == "wfs":
            formats = [
                e.text for e in root.iter() if local(e.tag) == "Format" and e.text
            ]
            for feature_type in root.iter():
                if local(feature_type.tag) != "FeatureType":
                    continue
                fields = {local(e.tag): (e.text or "").strip() for e in feature_type}
                name = fields.get("Name")
                if name:
                    item = {
                        "service": service,
                        "Name": name,
                        "Title": fields.get("Title", ""),
                        "DefaultCRS": fields.get(
                            "DefaultCRS", fields.get("DefaultSRS", "")
                        ),
                        "formats": formats,
                    }
                    records.append(item)
                    vectors[name] = item
        else:
            for coverage in root.iter():
                if local(coverage.tag) != "CoverageSummary":
                    continue
                fields = {local(e.tag): (e.text or "").strip() for e in coverage.iter()}
                name = fields.get("CoverageId") or fields.get("Identifier")
                if name:
                    records.append(
                        {
                            "service": service,
                            "Name": name,
                            "Title": fields.get("Title", ""),
                        }
                    )
    return records, vectors


def safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", nargs="?", default="geoserver-download", type=Path)
    parser.add_argument(
        "--delay", type=float, default=2.0, help="seconds between requests (default: 2)"
    )
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument(
        "--inventory-only", action="store_true", help="fetch and parse catalogs only"
    )
    args = parser.parse_args()
    out = args.output
    caps = out / "capabilities"
    data = out / "data"
    catalog = out / "catalog"
    for folder in (caps, data, catalog):
        folder.mkdir(parents=True, exist_ok=True)

    # Check direct file and catalog landing points before service transfers.
    # GeoServer installations commonly expose no public file browser; a 404 is OK.
    for path in ("", "web/", "rest/layers.json"):
        dest = catalog / (
            "landing.html" if not path else path.replace("/", "_") + "response"
        )
        fetch(f"{BASE}/{path}", dest, args.delay, args.timeout)

    urls = {
        "wms": f"{BASE}/wms?service=WMS&version=1.3.0&request=GetCapabilities",
        "wfs": f"{BASE}/wfs?service=WFS&version=2.0.0&request=GetCapabilities",
        "wcs": f"{BASE}/wcs?service=WCS&version=2.0.1&request=GetCapabilities",
    }
    for service, url in urls.items():
        fetch(url, caps / f"{service}.xml", args.delay, args.timeout)

    records, vectors = parse_capabilities(caps)
    (catalog / "layers-and-coverages.json").write_text(
        json.dumps(records, indent=2) + "\n"
    )
    if args.inventory_only:
        print(f"Inventory saved under {out}")
        return 0

    # Prefer a server-provided ZIP (native WFS archive) if the WFS advertises it.
    # Otherwise fetch GeoJSON, then store a GeoPackage copy for convenient use.
    wfs_formats = {f.lower() for item in vectors.values() for f in item["formats"]}
    for name in vectors:
        encoded = urllib.parse.urlencode(
            {
                "service": "WFS",
                "version": "2.0.0",
                "request": "GetFeature",
                "typeNames": name,
            }
        )
        stem = safe_name(name)
        archive_format = next(
            (f for f in wfs_formats if "shape-zip" in f or "shapefile" in f), None
        )
        if archive_format:
            url = f"{BASE}/wfs?{encoded}&outputFormat={urllib.parse.quote(archive_format, safe='')}"
            if fetch(url, data / f"{stem}.zip", args.delay, args.timeout):
                continue
        url = f"{BASE}/wfs?{encoded}&outputFormat=application%2Fjson"
        geojson = data / f"{stem}.geojson"
        if fetch(url, geojson, args.delay, args.timeout):
            result = subprocess.run(
                [
                    "gdal",
                    "vector",
                    "convert",
                    str(geojson),
                    str(data / f"{stem}.gpkg"),
                    "--format",
                    "GPKG",
                ],
                check=False,
            )
            if result.returncode:
                print(f"Could not convert {name} to GeoPackage", file=sys.stderr)

    # WCS catalogs can be downloaded at native resolution, but request syntax
    # varies between WCS versions. Keep the capabilities/coverage descriptions in
    # the inventory; next step is based on their advertised native envelopes and
    # formats rather than asking the server for an arbitrary large image.
    print(f"Inventory and WFS downloads saved under {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
