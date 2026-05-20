---
name: earthscience-stac
description: How to find and load data from Microsoft Planetary Computer (STAC) inside execute_earthscience_code. 
---

# Loading Planetary Computer Data

This domain ships a code-execution environment (rasterio, xarray, rioxarray,
geopandas, pystac-client, planetary-computer, numpy, pandas, scipy,
matplotlib auto-imported). The hard part of geospatial work is **finding
the right scene and loading it correctly** — once you have an array in
hand, write the analysis directly in `execute_earthscience_code`.

This skill covers everything specific to the Planetary Computer STAC API.
Raster math (indices, masking, statistics) is plain Python; do not look
for a recipe here.

## Always Sign the Catalog

Without `modifier=planetary_computer.sign_inplace`, asset URLs come back
unsigned and downloads return HTTP 403.

```python
catalog = pystac_client.Client.open(
    "https://planetarycomputer.microsoft.com/api/stac/v1",
    modifier=planetary_computer.sign_inplace,
)
```

Catalog enumeration alone (`get_collections`) does not require signing,
but always include it when you intend to read assets.

## Common Collections

| Collection ID | Holds |
|---|---|
| `sentinel-2-l2a` | Sentinel-2 surface reflectance, 10–60 m, 5-day revisit (optical) |
| `landsat-c2-l2` | Landsat 8/9 Collection 2 Level-2, 30 m, 16-day revisit (optical) |
| `sentinel-1-rtc` | Sentinel-1 radiometric terrain corrected SAR |
| `cop-dem-glo-30` | Copernicus DEM, 30 m global |
| `nasadem` | NASADEM, 30 m global |
| `naip` | NAIP aerial imagery (US, 0.6 m) |
| `io-lulc-9-class` | Esri 10 m global land cover |
| `chloris-biomass` | 30 m global biomass density |

If the user has not named a collection, search the catalog first:

```python
catalog = pystac_client.Client.open(
    "https://planetarycomputer.microsoft.com/api/stac/v1",
)
needle = "land cover"  # adapt to user intent
for c in catalog.get_collections():
    blob = f"{c.id} {c.title or ''} {c.description or ''}".lower()
    if needle.lower() in blob:
        print(c.id, "—", c.title)
```

## Bounding Boxes Are Lon/Lat in EPSG:4326

STAC bbox arguments are always `[west, south, east, north]` in lon/lat.
A frequent user mistake is copying a bbox from a Web Mercator viewer:

```python
# CORRECT — San Francisco Bay
bbox = [-122.5, 37.7, -122.3, 37.9]
# WRONG — Web Mercator metres, will return zero hits
bbox = [-13635000, 4540000, -13615000, 4565000]
```

If the user gives an address or place name, geocode first (e.g. with
`geopandas` and a known places dataset, or ask the user for a bbox).

## Search Scenes Over an AOI + Time Range

```python
catalog = pystac_client.Client.open(
    "https://planetarycomputer.microsoft.com/api/stac/v1",
    modifier=planetary_computer.sign_inplace,
)

search = catalog.search(
    collections=["sentinel-2-l2a"],
    bbox=[-122.5, 37.7, -122.3, 37.9],
    datetime="2024-06-01/2024-06-30",
    query={"eo:cloud_cover": {"lt": 10}},  # see thresholds below
    max_items=10,
)
items = list(search.items())
for item in items[:5]:
    print(item.id, item.datetime.date(), item.properties.get("eo:cloud_cover"))
```

`item.assets[KEY].href` is the signed URL passed to `rasterio.open` or
`rioxarray.open_rasterio`. The `query` dict accepts any STAC
query-extension predicate — common ones for Sentinel-2 / Landsat:

```python
query={
    "eo:cloud_cover": {"lt": 10},
    "s2:nodata_pixel_percentage": {"lt": 5},   # avoid edge-of-swath
    "platform": {"eq": "sentinel-2a"},
}
```

## Cloud-Cover Thresholds (Optical Collections)

| Use case | Threshold |
|---|---|
| Quicklook / visual inspection | 30 |
| Single-scene index work (NDVI etc.) | 10–20 |
| Median composite over months | 60 (more scenes survive) |

Always pass `eo:cloud_cover` for `sentinel-2-l2a` and `landsat-c2-l2`
when the downstream task expects usable pixels.

## Signed URLs Expire (~1 hour)

If a download or `rasterio.open` fails with HTTP 401/403 on a
previously-working href, re-mint the URL — don't re-run the full search:

```python
catalog = pystac_client.Client.open(
    "https://planetarycomputer.microsoft.com/api/stac/v1",
    modifier=planetary_computer.sign_inplace,
)
item = catalog.get_collection("sentinel-2-l2a").get_item(item_id)
fresh_href = item.assets["B04"].href
```

## Loading the Data

Once you have a signed href, use rasterio for a single band:

```python
with rasterio.open(item.assets["B04"].href) as src:
    arr = src.read(1, masked=True)   # numpy MaskedArray
    crs = src.crs
    transform = src.transform
```

Or rioxarray for a labeled-coords xarray (preserves CRS, coords,
spatial extent automatically):

```python
da = rioxarray.open_rasterio(item.assets["B04"].href, masked=True)
# da is an xarray DataArray with .rio.crs, .rio.bounds(), etc.
```

For multi-band stacks, open each band and concatenate, or use
`stackstac` (not in the default env; ask if you want it added).

## Memory Discipline

A full Sentinel-2 tile is ~10980×10980 per band — one float32 band is
~480 MB, NDVI from two bands at full res ≈ 1 GB working set. When
prototyping or producing previews, restrict to the AOI with a read
window and downsample on the fly:

```python
from rasterio.windows import from_bounds, Window
from rasterio.warp import transform_bounds

bbox_4326 = [-122.5, 37.7, -122.3, 37.9]
MAX_PIXELS = 1_000_000

with rasterio.open(item.assets["B04"].href) as src:
    src_bounds = transform_bounds("EPSG:4326", src.crs, *bbox_4326)
    window = from_bounds(*src_bounds, transform=src.transform).round_offsets().round_lengths()
    window = window.intersection(Window(0, 0, src.width, src.height))
    h, w = int(window.height), int(window.width)
    if h * w > MAX_PIXELS:
        scale = (MAX_PIXELS / (h * w)) ** 0.5
        out_h, out_w = max(1, int(h * scale)), max(1, int(w * scale))
    else:
        out_h, out_w = h, w
    arr = src.read(1, window=window, out_shape=(out_h, out_w), masked=True)
```

Reserve full-resolution reads for the final pass once the recipe works.

## Other Things Worth Knowing

- **Sentinel-2 L2A reflectance is stored as uint16 scaled by 10000.**
  For absolute reflectance divide by 10000 before formulas like EVI
  that have additive constants. For ratio indices like NDVI the scaling
  cancels — no division needed.
- **Geometries must be reprojected to the raster's CRS before
  `rasterio.mask.mask` or `rasterio.windows.from_bounds`.** Sentinel-2 /
  Landsat scenes are in UTM zones, not EPSG:4326. Use
  `rasterio.warp.transform_geom("EPSG:4326", src.crs, geom)` or
  `rasterio.warp.transform_bounds`.
- **`max_items` in `catalog.search` caps results.** Default is 100 on
  the server pager; the call returns fewer if there are fewer hits.
- **Public API is throttled** for anonymous access. Heavy time-series
  pulls may rate-limit — keep `max_items` reasonable and consider
  retries with backoff for large jobs.


