---
name: vegetation-monitoring
description: Compute NDVI from Sentinel-2 / Landsat imagery and summarise per polygon — useful for "mean NDVI per county / tract / parcel" tasks.
states:
  - earthscience.items_searched
  - earthscience.ndvi_computed
  - earthscience.raster_clipped
  - earthscience.zonal_stats_computed
---

# Vegetation Monitoring

Use this skill when the user wants to compute a vegetation index (NDVI is
the canonical case) over an AOI and summarise it per region. Builds on
[imagery-discovery](imagery-discovery.md) for the search step.

## State Graph

```
search_stac_items(collection, bbox, datetime, cloud_cover_lt)
    → earthscience.items_searched
            │
            ▼
compute_ndvi(red_href, nir_href, bbox?, max_pixels?)
    requires: earthscience.items_searched
    → earthscience.ndvi_computed
            │
            ├── clip_to_geometry(raster_path, geometry_geojson)
            │       → earthscience.raster_clipped
            │
            └── zonal_statistics(raster_path, polygons_geojson, stats?, id_field?)
                    → earthscience.zonal_stats_computed
```

## Tools

### compute_ndvi

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `red_href` | str | Yes | Red-band href (Sentinel-2 `B04`, Landsat `red`) |
| `nir_href` | str | Yes | NIR-band href (Sentinel-2 `B08`, Landsat `nir08`) |
| `bbox` | list | No | Subset bbox `[w,s,e,n]` in EPSG:4326 |
| `max_pixels` | int | No | Approximate cap on output pixels (default 1,000,000) |
| `output_path` | str | No | Auto-generated under `/tmp` if omitted |

**Returns:** `output_path`, `shape`, `crs`, `bounds`, `ndvi_min`,
`ndvi_max`, `ndvi_mean`, `ndvi_std`, `valid_pixels`, `total_pixels`.

NDVI is clipped to `[-1.0, 1.0]`; pixels where `NIR + Red == 0` become
NaN and the GeoTIFF's nodata.

### clip_to_geometry

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `raster_path` | str | Yes | Raster to clip |
| `geometry_geojson` | dict | Yes | GeoJSON Geometry/Feature/FeatureCollection (EPSG:4326) |
| `output_path` | str | No | Auto-generated under `/tmp` if omitted |
| `all_touched` | bool | No | Include any pixel touched by the geometry (default False) |

**Returns:** `output_path`, `shape`, `crs`, `bounds`, `input_bbox_4326`,
`num_features`.

### zonal_statistics

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `raster_path` | str | Yes | Raster to summarise |
| `polygons_geojson` | dict | Yes | GeoJSON Feature/FeatureCollection (EPSG:4326) |
| `stats` | list | No | Default `["mean","min","max","std","count"]` |
| `id_field` | str | No | Property to copy as row `id` (e.g. `GEOID`) |
| `band` | int | No | 1-based band index |
| `all_touched` | bool | No | Include any touched pixel |

**Returns:** `num_features`, `stats_requested`, `results` (per-polygon
dicts with `id`, `feature_index`, and one entry per requested stat).

## Workflow Example: Mean NDVI per parcel

```python
# Step 1: find a clear Sentinel-2 scene over the AOI
hits = search_stac_items(
    collection="sentinel-2-l2a",
    bbox=[-121.95, 37.30, -121.85, 37.40],
    datetime="2024-06-15/2024-06-30",
    cloud_cover_lt=10,
    max_items=1,
)
item = hits["items"][0]

# Step 2: compute NDVI from the red + NIR assets
ndvi = compute_ndvi(
    red_href=item["assets"]["B04"],
    nir_href=item["assets"]["B08"],
    bbox=[-121.95, 37.30, -121.85, 37.40],   # match the AOI
    max_pixels=500_000,                       # ~700×700 preview
)
print(f"NDVI mean over scene: {ndvi['ndvi_mean']:.3f} "
      f"(valid={ndvi['valid_pixels']}/{ndvi['total_pixels']})")

# Step 3: load parcel polygons (e.g. from the data lake or a GeoJSON file)
parcels = gpd.read_file("parcels.geojson")
parcels_geojson = parcels.__geo_interface__   # FeatureCollection

# Step 4: per-parcel mean NDVI
zonal = zonal_statistics(
    raster_path=ndvi["output_path"],
    polygons_geojson=parcels_geojson,
    stats=["mean", "count"],
    id_field="parcel_id",
)
import pandas as pd
df = pd.DataFrame(zonal["results"]).sort_values("mean", ascending=False)
print(df.head())
```

## Pitfalls

- **Cloudy NDVI is meaningless.** Always pass a `cloud_cover_lt` filter
  (≤ 10% for single-scene NDVI). If the only available scene is cloudy,
  prefer a median composite (PR 3 — `build_cloudless_mosaic`).
- **NDVI on a snowy or burned surface** can saturate to ±1; check the
  `valid_pixels / total_pixels` ratio before drawing conclusions.
- **CRS mismatch** between polygons and raster is handled internally
  (geometries are reprojected to the raster's UTM zone), but stats are
  computed in raster pixels — very small polygons may yield `count=0`.
  Use `all_touched=True` for sliver polygons.
- **Don't raise `max_pixels` blindly.** A full Sentinel-2 tile at
  10980×10980 = ~120 MP per band × 2 bands × float32 ≈ 1 GB working set.
  Restrict via `bbox` instead.
