---
name: imagery-discovery
description: Discover Planetary Computer STAC collections and items for an area + time range, then resolve signed asset URLs. The "always first" earthscience workflow.
states:
  - earthscience.items_searched
  - earthscience.assets_resolved
---

# Imagery Discovery

Use this skill at the start of any earthscience task to (a) confirm which
collection holds the data you need, (b) find specific scenes over the user's
AOI and time range, and (c) get short-lived signed URLs for downstream raster
operations.

## State Graph

```
list_collections(search?)            # optional — picks the right collection
search_stac_items(collection, bbox, datetime, cloud_cover_lt?)
    → earthscience.items_searched

get_item_assets(collection, item_id, asset_keys?)
    requires: earthscience.items_searched
    → earthscience.assets_resolved
```

## Tools

### list_collections

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `search` | str | No | Substring filter on id/title/description |
| `max_results` | int | No | Cap on returned collections (default 50) |

**Returns:** `num_total`, `num_returned`, `collections`
(list of `id`, `title`, `description`, `spatial_extent`, `temporal_extent`,
`license`, `keywords`).

Skip this tool if the user has explicitly named a collection (e.g.
"Sentinel-2 L2A"). Use it when they say "satellite imagery" or "elevation
data" without specifying which dataset.

### search_stac_items

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `collection` | str | Yes | Collection ID (e.g. `sentinel-2-l2a`) |
| `bbox` | list | No | `[west, south, east, north]` in EPSG:4326 |
| `datetime` | str | No | Date or `start/end` range |
| `cloud_cover_lt` | float | No | Max cloud cover percentage |
| `query` | dict | No | Extra STAC query-extension filters |
| `max_items` | int | No | Default 25, hard limit 200 |

**Returns:** `num_items`, `collection`, `items` (list with `id`, `datetime`,
`bbox`, `cloud_cover`, `platform`, `assets` mapping asset key → signed URL).

### get_item_assets

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `collection` | str | Yes | Collection ID |
| `item_id` | str | Yes | Item ID from `search_stac_items` |
| `asset_keys` | list | No | Subset of asset keys (e.g. `["B04","B08"]`) |

**Returns:** `item_id`, `collection`, `datetime`, `bbox`, `assets`
(dict mapping key → `{href, type, title, roles}`).

## Common Collections on Planetary Computer

| Collection ID | What it holds |
|---|---|
| `sentinel-2-l2a` | Sentinel-2 surface reflectance, 10–60 m, 5-day revisit |
| `landsat-c2-l2` | Landsat 8/9 Collection 2 Level-2, 30 m, 16-day revisit |
| `sentinel-1-rtc` | Sentinel-1 radiometric terrain corrected SAR |
| `cop-dem-glo-30` | Copernicus DEM, 30 m global |
| `nasadem` | NASADEM, 30 m global |
| `naip` | NAIP aerial imagery (US, 0.6 m) |
| `io-lulc-9-class` | Esri 10 m global land cover |
| `chloris-biomass` | 30 m global biomass density |

## Workflow Example

```python
# Step 1 (optional): confirm the right collection
catalog = list_collections(search="sentinel-2")
print([c["id"] for c in catalog["collections"]])
# → ['sentinel-2-l2a']

# Step 2: search for clear scenes over the AOI + time window
hits = search_stac_items(
    collection="sentinel-2-l2a",
    bbox=[-122.5, 37.7, -122.3, 37.9],   # San Francisco
    datetime="2024-06-01/2024-06-30",
    cloud_cover_lt=10,
    max_items=10,
)
print(f"{hits['num_items']} scenes")
for it in hits["items"][:3]:
    print(f"  {it['id']} ({it['datetime']}) cloud={it['cloud_cover']:.1f}%")

# Step 3 (optional): refresh signed URLs for a specific item later
fresh = get_item_assets(
    collection="sentinel-2-l2a",
    item_id=hits["items"][0]["id"],
    asset_keys=["B04", "B08"],
)
red = fresh["assets"]["B04"]["href"]
nir = fresh["assets"]["B08"]["href"]
```

## Pitfalls

- Empty result set → check (a) `bbox` is in lon/lat order, (b) `datetime`
  range overlaps the collection's temporal extent, (c) `cloud_cover_lt`
  isn't too aggressive for the season/region.
- Signed URLs expire after ~1 hour. If a later tool fails with HTTP
  401/403 on a previously-working asset, re-run `get_item_assets`.
- `max_items` defaults to 25; raise it only after confirming the
  result count is what you expect — large pages are slow.
