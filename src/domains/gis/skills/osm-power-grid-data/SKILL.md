---
name: osm-power-grid-data
description: Pull electrical power grid data (transmission lines, substations, generators) from OpenStreetMap using the Overpass API and convert it to GeoJSON for geospatial analysis.
---

# OpenStreetMap Power Grid Data Extraction

Use this skill when you need geospatial power grid data — transmission lines,
substations, generators, or other electrical infrastructure — from
OpenStreetMap (OSM). OSM is a free, community-maintained geodatabase with
global coverage. Data is accessed via the **Overpass API**, a read-only query
interface that returns structured JSON or XML.

## When to Use

- You need real geometry for transmission lines (not just endpoint coordinates)
- You need substation locations with names, voltages, and operators
- You want openly licensed data (ODbL) with no API key required
- You need data for a specific geographic region (state, country, bounding box)

## Overpass API Basics

**Endpoint**: `https://overpass-api.de/api/interpreter`
**Method**: POST with `data=<query>` parameter
**Output**: JSON (`[out:json]`) or XML (default)
**Rate limit**: ~2 requests/minute on the public server; use timeouts and retries

### Query Structure

```
[out:json][timeout:600];
<area or bbox filter>;
(
  <element selectors>;
);
out body;
>;
out skel qt;
```

- `out body` — returns elements with tags
- `>` — recursion: fetches all child nodes of returned ways (needed to get coordinates)
- `out skel qt` — returns child nodes with only coordinates (no tags), sorted spatially

## OSM Power Tags

OSM uses `power=*` tags for electrical infrastructure:

| Tag | Description | Geometry Type |
|-----|-------------|---------------|
| `power=line` | High-voltage transmission line | Way (LineString) |
| `power=cable` | Underground or submarine power cable | Way (LineString) |
| `power=substation` | Electrical substation | Node (Point) or Way (Polygon) |
| `power=station` | Older tag for substation (still in use) | Node or Way |
| `power=plant` | Power plant / generating station | Node or Way |
| `power=generator` | Individual generator unit | Node |
| `power=tower` | Transmission tower/pylon | Node |
| `power=pole` | Distribution pole | Node |

### Key Properties on Power Lines

| OSM Tag | Description | Example |
|---------|-------------|---------|
| `voltage` | Operating voltage in **volts** | `230000`, `115000;230000` |
| `cables` | Number of physical cables | `3`, `6` |
| `circuits` | Number of circuits on the structure | `1`, `2` |
| `operator` | Company operating the line | `Dominion Energy` |
| `name` | Line name/identifier | `Mt Storm - Meadow Brook` |
| `ref` | Reference number | `502` |

### Key Properties on Substations

| OSM Tag | Description | Example |
|---------|-------------|---------|
| `name` | Substation name | `Goose Creek Substation` |
| `voltage` | Voltage level(s) in volts | `500000`, `230000;115000` |
| `operator` | Operating company | `Dominion Energy Virginia` |
| `substation` | Substation type | `transmission`, `distribution` |

## Querying by State/Region

Use **area filters** to query by administrative boundary (more accurate than
bounding boxes):

```overpass
[out:json][timeout:600];
area["name"="Virginia"]["admin_level"="4"]->.a0;
area["name"="Maryland"]["admin_level"="4"]->.a1;
(
  way["power"="line"](area.a0);
  way["power"="line"](area.a1);
);
out body;
>;
out skel qt;
```

- `admin_level=2` → country
- `admin_level=4` → state/province (US)
- `admin_level=6` → county (US)
- `admin_level=8` → city/town

### Querying by Bounding Box

For simpler geographic filtering:

```overpass
[out:json][timeout:300];
(
  way["power"="line"](33.8,-83.0,40.0,-75.0);
);
out body;
>;
out skel qt;
```

Format: `(south,west,north,east)` in decimal degrees (WGS84).

## Converting Overpass JSON to GeoJSON

Overpass returns nodes and ways separately. To build GeoJSON LineStrings:

1. **Index all nodes** by ID → `{node_id: (lon, lat)}`
2. **Iterate ways** that have `power=line` in their tags
3. **Resolve coordinates** from the way's node list using the node index
4. **Parse voltage**: divide by 1000 to convert volts → kV; handle semicolons for multi-voltage lines

### Voltage Parsing Helper

```python
import re

def parse_voltage_kv(voltage: str) -> list[float]:
    """
    Parse an OSM voltage tag into a list of kilovolt (kV) values.
    Examples of voltage tag formats:
      - "110000"
      - "110000;220000"
      - "110;220"
    """
    if not voltage:
        return []
    kv_values = []
    for part in re.split(r"[;,\s]+", voltage):
        if not part:
            continue
        try:
            value = float(part)
        except ValueError:
            continue
        # If value looks like volts (e.g. 110000), convert to kV.
        if value > 1000:
            value = value / 1000.0
        kv_values.append(value)
    return kv_values
```

### Transmission Lines

```python
def overpass_to_geojson_lines(data: dict) -> dict:
    nodes = {}
    for el in data["elements"]:
        if el["type"] == "node":
            nodes[el["id"]] = (el["lon"], el["lat"])

    features = []
    for el in data["elements"]:
        if el["type"] != "way":
            continue
        tags = el.get("tags", {})
        if tags.get("power") not in ("line", "cable"):
            continue

        coords = [list(nodes[nid]) for nid in el.get("nodes", []) if nid in nodes]
        if len(coords) < 2:
            continue

        kv_values = parse_voltage_kv(tags.get("voltage", ""))
        voltage_kv = max(kv_values) if kv_values else 0

        features.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coords},
            "properties": {
                "osm_id": el["id"],
                "voltage_kv": voltage_kv,
                "name": tags.get("name", ""),
                "operator": tags.get("operator", ""),
            },
        })

    return {"type": "FeatureCollection", "features": features}
```

### Substations

For substations (which can be nodes or polygons), convert ways to their
centroid:

```python
def overpass_to_geojson_substations(data: dict) -> dict:
    nodes = {}
    for el in data["elements"]:
        if el["type"] == "node":
            nodes[el["id"]] = (el["lon"], el["lat"])

    features = []
    for el in data["elements"]:
        tags = el.get("tags", {})
        if tags.get("power") not in ("substation", "station"):
            continue

        if el["type"] == "node":
            lon, lat = el["lon"], el["lat"]
        elif el["type"] == "way":
            way_coords = [nodes[nid] for nid in el.get("nodes", []) if nid in nodes]
            if not way_coords:
                continue
            lon = sum(c[0] for c in way_coords) / len(way_coords)
            lat = sum(c[1] for c in way_coords) / len(way_coords)
        else:
            continue

        kv_values = parse_voltage_kv(tags.get("voltage", ""))
        voltage_kv = max(kv_values) if kv_values else 0

        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {
                "osm_id": el["id"],
                "name": tags.get("name", ""),
                "voltage_kv": voltage_kv,
                "operator": tags.get("operator", ""),
            },
        })

    return {"type": "FeatureCollection", "features": features}
```

## Deriving Line Endpoint Substation Names

OSM power lines do **not** have endpoint substation names as attributes (unlike
HIFLD shapefiles which have `SUB_1`/`SUB_2`). To determine which substations a
line connects:

1. Load substations into a spatial index (grid-based or R-tree)
2. For each line, get its first and last coordinate
3. Find the nearest named substation within ~5 km of each endpoint
4. Prefer voltage-compatible substations (same kV class)

```python
from collections import defaultdict

def build_substation_index(substations):
    """Grid-based spatial index for fast nearest-neighbor lookup."""
    grid = defaultdict(list)
    for i, sub in enumerate(substations):
        key = (int(sub.lat * 10), int(sub.lon * 10))
        grid[key].append(i)
    return grid

def find_nearest_substation(lon, lat, voltage_kv, grid, substations):
    """Find nearest named, voltage-compatible substation within ~5km.

    Prefers substations that share a kV class with the line. Falls back
    to the closest named substation regardless of voltage if no
    voltage-compatible match is found.
    """
    gk = (int(lat * 10), int(lon * 10))
    max_dist = 0.05  # ~5km in degrees
    best_name, best_dist = "", max_dist
    best_v_name, best_v_dist = "", max_dist  # voltage-compatible best

    for dlat in range(-1, 2):
        for dlon in range(-1, 2):
            for idx in grid.get((gk[0] + dlat, gk[1] + dlon), []):
                sub = substations[idx]
                dist = ((lat - sub.lat)**2 + (lon - sub.lon)**2) ** 0.5
                if dist >= max_dist or not sub.name:
                    continue
                # Track overall nearest named substation
                if dist < best_dist:
                    best_dist = dist
                    best_name = sub.name
                # Track nearest voltage-compatible substation
                if voltage_kv and any(
                    abs(v - voltage_kv) / max(voltage_kv, 1) < 0.1
                    for v in sub.voltage_kvs
                ):
                    if dist < best_v_dist:
                        best_v_dist = dist
                        best_v_name = sub.name

    return best_v_name or best_name
```

## Best Practices

### Rate Limiting and Retries
- Add **30–60 second delays** between queries
- Use **exponential backoff** on 429/503 responses
- Set `[timeout:600]` for large state-level queries
- Cache results locally — delete to re-download

### Data Quality Notes
- **~85% of US transmission lines** have voltage tags
- **~32% of substations** have names (the rest are unnamed distribution substations)
- Coverage varies by region; well-mapped in the Eastern US
- Some rural substations or recently built infrastructure may be missing
- Voltage data is **community-contributed** — cross-validate against utility data

### Coordinate System
- OSM data is in **WGS84 (EPSG:4326)** — no projection conversion needed
- Unlike HIFLD shapefiles (EPSG:3857 Web Mercator), OSM coordinates are ready
  to use directly in Leaflet, GeoJSON, or any standard GIS tool

### Python Dependencies
- **`httpx`** — HTTP client for Overpass API requests (async-capable); pre-installed in the GIS environment
- No OSM-specific libraries needed; raw JSON parsing is sufficient

## Other Useful Overpass Queries

### Power plants in a state
```overpass
[out:json][timeout:300];
area["name"="Virginia"]["admin_level"="4"]->.a;
(
  way["power"="plant"](area.a);
  node["power"="plant"](area.a);
);
out body;
>;
out skel qt;
```

### Transmission lines by voltage
```overpass
[out:json][timeout:300];
area["name"="Virginia"]["admin_level"="4"]->.a;
(
  way["power"="line"]["voltage"~"^(230000|500000)"](area.a);
);
out body;
>;
out skel qt;
```

### All electrical infrastructure in a bounding box
```overpass
[out:json][timeout:300];
(
  way["power"](37.0,-78.5,39.0,-76.0);
  node["power"](37.0,-78.5,39.0,-76.0);
);
out body;
>;
out skel qt;
```
