gdal raster mosaic $(find ortho-tiles -type f -name '*0.tif' -print) ortho.vrt --format VRT
gdal raster convert --format COG --overwrite --co COMPRESS=LZW --co BIGTIFF=YES ortho.vrt ortho.tif
