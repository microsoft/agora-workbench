# Supported Geospatial Formats

## Vector Formats

| Format | Extension | Read Function | Notes |
|--------|-----------|--------------|-------|
| Shapefile | `.shp` (+ .shx, .dbf, .prj) | `gpd.read_file()` | Most common; multi-file bundle |
| GeoJSON | `.geojson`, `.json` | `gpd.read_file()` | Web-friendly; always WGS84 |
| GeoPackage | `.gpkg` | `gpd.read_file(layer=...)` | Modern SQLite-based; multi-layer |
| KML / KMZ | `.kml`, `.kmz` | `gpd.read_file(driver="KML")` | Google Earth format |
| File Geodatabase | `.gdb/` | `gpd.read_file(layer=...)` | Esri format; read-only in Fiona |
| CSV with geometry | `.csv` | `pd.read_csv()` + `gpd.GeoDataFrame()` | Needs manual geometry construction |
| GeoParquet | `.parquet` | `gpd.read_parquet()` | Fast columnar format |

## Raster Formats

| Format | Extension | Read Function | Notes |
|--------|-----------|--------------|-------|
| GeoTIFF | `.tif`, `.tiff` | `rasterio.open()` | Most common raster format |
| NetCDF | `.nc` | `rasterio.open()` or `xarray` | Climate / scientific data |
| JPEG2000 | `.jp2` | `rasterio.open()` | Compressed satellite imagery |
| ASCII Grid | `.asc` | `rasterio.open()` | Simple text-based grid |

## Format Detection from File Extension

```python
VECTOR_EXTENSIONS = {".shp", ".geojson", ".json", ".gpkg", ".kml", ".kmz", ".gdb", ".parquet"}
RASTER_EXTENSIONS = {".tif", ".tiff", ".nc", ".jp2", ".asc"}

def detect_format(path):
    ext = Path(path).suffix.lower()
    if ext in VECTOR_EXTENSIONS:
        return "vector"
    elif ext in RASTER_EXTENSIONS:
        return "raster"
    return "unknown"
```
