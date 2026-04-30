---
name: planetary-computer
description: Search and load satellite imagery, land cover, elevation, and other geospatial datasets from Microsoft Planetary Computer via STAC API.
---

# Planetary Computer

Use this skill when the user asks for satellite imagery, land cover, elevation (DEM),
building footprints, census data, or any public geospatial dataset that isn't in the
data lake. Planetary Computer hosts 100+ open datasets with a free, unauthenticated
STAC API.

## Setup

```python
import planetary_computer
import pystac_client

catalog = pystac_client.Client.open(
    "https://planetarycomputer.microsoft.com/api/stac/v1",
    modifier=planetary_computer.sign_inplace,
)
```

The `modifier=planetary_computer.sign_inplace` handles token signing automatically —
no API keys needed.

## Searching for Data

Use `catalog.search()` with a bounding box, time range, and collection name:

```python
search = catalog.search(
    collections=["sentinel-2-l2a"],
    bbox=[-79.5, 37.0, -78.5, 38.0],   # [west, south, east, north]
    datetime="2024-06-01/2024-06-30",
    query={"eo:cloud_cover": {"lt": 20}},
    max_items=10,
)

items = search.item_collection()
print(f"Found {len(items)} items")

# Inspect the first item
item = items[0]
print(f"Date: {item.datetime}")
print(f"Assets: {list(item.assets.keys())}")
print(f"Cloud cover: {item.properties.get('eo:cloud_cover')}%")
```

## Loading Raster Data

Use `rioxarray` for loading into xarray (preferred) or `rasterio` for lower-level access:

```python
import rioxarray

# Load a single band
item = items[0]
da = rioxarray.open_rasterio(item.assets["B04"].href)  # Red band
print(f"Shape: {da.shape}, CRS: {da.rio.crs}")

# Load and clip to area of interest
da_clipped = da.rio.clip_box(
    minx=-79.2, miny=37.2, maxx=-78.8, maxy=37.6,
    crs="EPSG:4326"
)
```

For multiple bands (e.g., RGB composite):

```python
import numpy as np

red = rioxarray.open_rasterio(item.assets["B04"].href).squeeze()
green = rioxarray.open_rasterio(item.assets["B03"].href).squeeze()
blue = rioxarray.open_rasterio(item.assets["B02"].href).squeeze()

rgb = np.stack([red.values, green.values, blue.values], axis=-1)
```

## Loading Vector Data

Some collections return vector data (GeoJSON, GeoParquet):

```python
import geopandas as gpd

# Building footprints
search = catalog.search(
    collections=["ms-buildings"],
    bbox=[-79.5, 37.0, -78.5, 38.0],
)
items = search.item_collection()
gdf = gpd.read_parquet(items[0].assets["data"].href)
```

## Common Collections

| Collection | Description | Type | Key Assets/Bands |
|---|---|---|---|
| `sentinel-2-l2a` | Sentinel-2 L2A (10m multispectral) | Raster | B02 (Blue), B03 (Green), B04 (Red), B08 (NIR), SCL (scene class) |
| `landsat-c2-l2` | Landsat Collection 2 Level-2 (30m) | Raster | blue, green, red, nir08, lwir11, qa_pixel |
| `cop-dem-glo-30` | Copernicus DEM (30m elevation) | Raster | data |
| `cop-dem-glo-90` | Copernicus DEM (90m elevation) | Raster | data |
| `naip` | NAIP aerial imagery (US, 1m) | Raster | image (4-band RGBIR) |
| `io-lulc-annual-v02` | ESRI land use / land cover (10m) | Raster | data |
| `ms-buildings` | Microsoft building footprints | Vector | data (GeoParquet) |
| `us-census` | US Census boundaries | Vector | data |
| `io-biodiversity` | Biodiversity intactness index | Raster | data |
| `chloris-biomass` | Above-ground biomass | Raster | data |

## Key Patterns

### Filter by cloud cover (optical imagery)

```python
search = catalog.search(
    collections=["sentinel-2-l2a"],
    bbox=bbox,
    datetime=date_range,
    query={"eo:cloud_cover": {"lt": 10}},
    sortby=[{"field": "properties.eo:cloud_cover", "direction": "asc"}],
    max_items=5,
)
```

### Load elevation / DEM

```python
search = catalog.search(
    collections=["cop-dem-glo-30"],
    bbox=bbox,
)
items = search.item_collection()
dem = rioxarray.open_rasterio(items[0].assets["data"].href)
```

### Compute NDVI from Sentinel-2

```python
nir = rioxarray.open_rasterio(item.assets["B08"].href).squeeze().astype(float)
red = rioxarray.open_rasterio(item.assets["B04"].href).squeeze().astype(float)
ndvi = (nir - red) / (nir + red)
```

### Reproject raster to match vector CRS

```python
dem_reprojected = dem.rio.reproject(gdf.crs)
```

### Clip raster to polygon boundary

```python
from shapely.geometry import mapping
clipped = da.rio.clip([mapping(gdf.geometry.unary_union)], gdf.crs)
```

### Save raster for GUI display

The GUI supports raster layers natively. Save a GeoTIFF to `/tmp/maps/` and add it
as a "type": "raster" layer in map\_state.json:

```python
# Reproject to WGS84 for web display
ndvi_4326 = ndvi.rio.reproject("EPSG:4326")
ndvi_4326.rio.to_raster("/tmp/maps/ndvi.tif")

# In map_state.json layers:
{
    "id": "ndvi",
    "name": "NDVI",
    "type": "raster",
    "tif_file": "ndvi.tif",
    "colormap": "RdYlGn",
    "value_range": [-1, 1],
    "opacity": 0.7,
    "description": "NDVI from Sentinel-2 (June 2024)",
    "source": "Planetary Computer sentinel-2-l2a"
}
```

For RGB composites, save as 3-band GeoTIFF — the GUI renders it as RGB automatically:

```python
import numpy as np
import rioxarray

rgb = np.stack([red.values, green.values, blue.values])
rgb_da = xr.DataArray(rgb, dims=["band", "y", "x"], coords=red.coords)
rgb_da = rgb_da.rio.write_crs(red.rio.crs)
rgb_da = rgb_da.rio.reproject("EPSG:4326")
rgb_da.rio.to_raster("/tmp/maps/satellite_rgb.tif")
```

## Gotchas

- **Always sign URLs**: Use `modifier=planetary_computer.sign_inplace` when opening the catalog. Unsigned URLs will return 403.
- **Large rasters**: Don't load full Sentinel-2 tiles into memory at once — clip to your area of interest first with `rio.clip_box()`.
- **CRS mismatch**: Sentinel-2 and Landsat tiles use UTM zones. Reproject to EPSG:4326 before overlaying with vector data, or reproject vectors to match the raster CRS.
- **Band scaling**: Sentinel-2 L2A reflectance values are scaled by 10000. Divide by 10000 for true reflectance (0-1 range).
- **Cloud masking**: Use the SCL (Scene Classification Layer) band for Sentinel-2 cloud masking. Values 4 (vegetation) and 5 (bare soil) are cloud-free.
- **No results?**: Check that your bbox and datetime range are valid. Some collections have limited temporal coverage. Use `catalog.get_collection("collection-id")` to check extent.
