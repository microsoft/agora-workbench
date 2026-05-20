---
name: earthscience-stac
description: Geospatial discovery and raster analysis on Microsoft Planetary Computer — STAC search, signed asset URLs, NDVI computation, clipping, and zonal statistics via domain tools and the execute_earthscience_code tool.
states:
  - earthscience.items_searched
  - earthscience.assets_resolved
  - earthscience.ndvi_computed
  - earthscience.raster_clipped
  - earthscience.zonal_stats_computed
---

# Earth Science / Planetary Computer

Use this skill when the user asks about satellite imagery, vegetation indices,
remote sensing, geospatial analysis, or any task involving the Microsoft
Planetary Computer STAC catalog. Code runs in the `execute_earthscience_code`
tool with `pystac_client`, `planetary_computer`, `rasterio`, `xarray`,
`rioxarray`, `geopandas`, `shapely`, `numpy`, `pandas` auto-imported.

## State Graph Overview

```
search_stac_items / list_collections
    → earthscience.items_searched
            │
            ├── get_item_assets → earthscience.assets_resolved
            │
            └── compute_ndvi → earthscience.ndvi_computed
                    │
                    ├── clip_to_geometry → earthscience.raster_clipped
                    │
                    └── zonal_statistics → earthscience.zonal_stats_computed
```

## Workflow Skills

| Skill | Tools | Description |
|-------|-------|-------------|
| [imagery-discovery](imagery-discovery.md) | `list_collections` → `search_stac_items` → `get_item_assets` | Find scenes over an AOI and resolve signed asset URLs |
| [vegetation-monitoring](vegetation-monitoring.md) | `search_stac_items` → `compute_ndvi` → `clip_to_geometry` → `zonal_statistics` | NDVI per polygon, e.g. mean NDVI per county |

## Auto-Imported Modules

Available without explicit imports inside `execute_earthscience_code`:

```python
import planetary_computer
import pystac_client
import rasterio
import xarray as xr
import rioxarray
import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import box, Point, Polygon
```

## Critical: Signed URLs Expire

Planetary Computer signed asset URLs are short-lived (~1 hour). If a
downstream tool fails with HTTP 401/403 on a previously-working href,
re-run `get_item_assets(collection, item_id)` to mint a fresh URL.

## Critical: Bounding Boxes Are Lon/Lat

`bbox` parameters across these tools are **always** in EPSG:4326 (longitude,
latitude) ordered `[west, south, east, north]`. Common pitfall: the user
may copy a bbox from a metric CRS — confirm before searching.

```python
# CORRECT — San Francisco Bay
bbox = [-122.5, 37.7, -122.3, 37.9]

# WRONG — looks plausible but is in Web Mercator metres
bbox = [-13635000, 4540000, -13615000, 4565000]
```

## Cloud Cover Filtering

For optical collections (Sentinel-2, Landsat) always pass `cloud_cover_lt`
when the user wants usable imagery. Typical thresholds:

| Use case | Threshold |
|----------|-----------|
| Visual inspection / quicklook | 30 |
| Single-scene NDVI | 10–20 |
| Median composite over months | 60 (more scenes survive) |

## Memory Discipline for Raster Reads

Sentinel-2 tiles are ~10980×10980 per band. Tools default to
`max_pixels=1_000_000` — a downsample the agent can override per call.
For full-resolution work, restrict via `bbox` instead of raising
`max_pixels`.
