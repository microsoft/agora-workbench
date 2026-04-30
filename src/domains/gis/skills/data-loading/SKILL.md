---
name: data-loading
description: Load geospatial data from data lake assets into GeoPandas or Rasterio, with format detection and initial inspection.
---

# Data Loading

Use this skill when the user provides a data lake asset (shapefile, GeoJSON, GeoPackage,
GeoTIFF, Excel, etc.) or when you need to load geospatial data into the session for analysis.

## Determining the File Type

Data lake assets are resolved to a local `Path`. **Check the file extension first** to
decide how to handle it — not all assets are zip files.

```python
from pathlib import Path

asset = Path(asset_path)
ext = asset.suffix.lower()
print(f"File: {asset.name}, Extension: {ext}")
```

## Direct Loading (non-zip files)

Most data lake assets can be loaded directly without extraction:

```python
import geopandas as gpd
import pandas as pd

if ext == ".shp":
    gdf = gpd.read_file(asset)
elif ext in (".geojson", ".json"):
    gdf = gpd.read_file(asset)
elif ext == ".gpkg":
    gdf = gpd.read_file(asset)
elif ext in (".xlsx", ".xls"):
    df = pd.read_excel(asset)
elif ext == ".csv":
    df = pd.read_csv(asset)
elif ext in (".tif", ".tiff"):
    import rasterio
    src = rasterio.open(asset)
```

## Zip File Extraction

**Only extract if the file is a zip archive.** Shapefiles bundled as zip files are
the most common case. Use `zipfile.namelist()` to list contents without `rglob`:

```python
import zipfile
import tempfile
import shutil
from pathlib import Path

if ext == ".zip":
    extract_dir = tempfile.mkdtemp()
    extract_root = Path(extract_dir).resolve()
    with zipfile.ZipFile(asset, "r") as zf:
        names = zf.namelist()
        print(f"Zip contents: {names}")

        # Safely extract each member, preventing zip-slip path traversal
        for name in names:
            dest_path = (extract_root / name).resolve()
            if not str(dest_path).startswith(str(extract_root)):
                raise ValueError(f"Unsafe path in zip member: {name}")

            if name.endswith("/"):
                # Directory entry
                dest_path.mkdir(parents=True, exist_ok=True)
                continue

            dest_path.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(name) as src, open(dest_path, "wb") as dst:
                shutil.copyfileobj(src, dst)

    # Find the target file from the known filenames
    extracted = extract_root
    shp_files = [extracted / n for n in names if n.endswith(".shp")]
    geojson_files = [extracted / n for n in names if n.endswith((".geojson", ".json"))]
    gpkg_files = [extracted / n for n in names if n.endswith(".gpkg")]
    tif_files = [extracted / n for n in names if n.endswith((".tif", ".tiff"))]
    xlsx_files = [extracted / n for n in names if n.endswith((".xlsx", ".xls"))]
```

## Loading Vector Data

Use GeoPandas for vector formats (shapefiles, GeoJSON, GeoPackage):

```python
import geopandas as gpd

gdf = gpd.read_file(shp_files[0])  # or geojson_files[0], gpkg_files[0]
```

For GeoPackage with multiple layers, list and select:

```python
import fiona
layers = fiona.listlayers(gpkg_files[0])
gdf = gpd.read_file(gpkg_files[0], layer=layers[0])
```

## Creating a GeoDataFrame from Tabular Data

For Excel or CSV files with latitude/longitude columns:

```python
import pandas as pd
import geopandas as gpd

df = pd.read_excel(asset)  # or pd.read_csv(asset)
gdf = gpd.GeoDataFrame(
    df,
    geometry=gpd.points_from_xy(df["Longitude"], df["Latitude"]),
    crs="EPSG:4326",
)
```

## Loading Raster Data

Use Rasterio for raster formats (GeoTIFF, etc.):

```python
import rasterio

with rasterio.open(asset) as src:
    data = src.read(1)  # first band
    bounds = src.bounds
    crs = src.crs
    transform = src.transform
```

## Initial Inspection Checklist

After loading any dataset, always run this inspection to understand what you have:

**Vector data:**
```python
print(f"CRS: {gdf.crs}")
print(f"Shape: {gdf.shape}")
print(f"Geometry types: {gdf.geometry.type.unique()}")
print(f"Bounds: {gdf.total_bounds}")  # [minx, miny, maxx, maxy]
print(f"Columns: {list(gdf.columns)}")
print(gdf.head())
```

**Raster data:**
```python
with rasterio.open(asset) as src:
    print(f"CRS: {src.crs}")
    print(f"Size: {src.width} x {src.height}")
    print(f"Bands: {src.count}")
    print(f"Bounds: {src.bounds}")
    print(f"Resolution: {src.res}")
```

## Shapefile Bundle Handling

Shapefiles consist of multiple companion files — all are required:

| Extension | Purpose | Required |
|-----------|---------|----------|
| `.shp`    | Geometry | Yes |
| `.shx`    | Spatial index | Yes |
| `.dbf`    | Attribute data | Yes |
| `.prj`    | Coordinate system | Yes |
| `.cpg`    | Character encoding | No |

When extracting a zip, all companion files are extracted together. Pass only the `.shp`
path to `gpd.read_file()` — it locates the other files automatically.

## Common Pitfalls

- **Don't assume zip**: Check the file extension first. Excel files (`.xlsx`), GeoJSON, and CSV can be loaded directly.
- **Don't use `rglob` or `glob` on extracted directories**: The sandbox may block these. Instead, use `zipfile.namelist()` to get filenames, then construct paths directly.
- **Missing .prj file**: The CRS will be `None`. Ask the user or check documentation for the correct CRS, then assign it: `gdf = gdf.set_crs(epsg=4326)`.
- **Encoding issues**: If attribute text is garbled, try: `gpd.read_file(path, encoding="utf-8")` or `encoding="latin-1"`.
- **Large files**: For very large datasets, use `bbox` or `rows` parameter to load a subset: `gpd.read_file(path, rows=1000)`.

See [references/supported-formats.md](references/supported-formats.md) for a full table of
supported formats.
