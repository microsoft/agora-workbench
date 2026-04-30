# Data Fetching: EIA Demand, EIA Plants, OSM

## Overview

The grid construction pipeline needs external data:

| Data | Source | API | Purpose |
|------|--------|-----|---------|
| EIA-930 demand | U.S. EIA | EIA API v2 | Hourly BA-level demand for load allocation |
| EIA-860 plants | U.S. EIA | EIA API v2 | Generator capacity, fuel type, location |
| OSM power features | OpenStreetMap | Overpass API | Backup plant matching (capacity, fuel) |

## 1. EIA-930 Hourly Demand

Provides hourly demand for a Balancing Authority (BA) on a given date.

**API endpoint:** `https://api.eia.gov/v2/electricity/rto/region-data/data/`

**Request parameters:**
```python
params = {
    "frequency": "hourly",
    "data[0]": "value",
    "facets[respondent][]": ba_code,   # e.g. "PJM", "MISO", "ERCO"
    "facets[type][]": "D",             # D = Demand
    "start": f"{date}T00",             # e.g. "2024-07-15T00"
    "end": f"{date}T23",
    "sort[0][column]": "period",
    "sort[0][direction]": "asc",
    "length": 100,
}
if api_key:
    params["api_key"] = api_key
```

**Response parsing:** Records are in `response["response"]["data"]`. Each row has `"period"` (e.g. `"2024-07-15T16"`) and `"value"` (demand in MW).

**Output format** (cache as JSON):
```json
{
  "ba_code": "PJM",
  "date": "2024-07-15",
  "records": [
    {"hour": 0, "demand_mw": 95000},
    {"hour": 16, "demand_mw": 150618}
  ]
}
```

**Common BA codes:** PJM, MISO, ERCO (ERCOT), ISNE (ISO-NE), NYIS (NYISO), CISO (CAISO), SPP, SWPP

**API key:** Free at https://www.eia.gov/opendata/register.php. Optional but recommended (rate-limited without one).

## 2. EIA-860 Plant Registry

Provides operating generator capacity, fuel type, and location by state.

**API endpoint:** `https://api.eia.gov/v2/electricity/operating-generator-capacity/data/`

**Request parameters:**
```python
params = {
    "frequency": "annual",
    "data[0]": "nameplate-capacity-mw",
    "facets[stateid][]": state_abbrev,  # e.g. "VA"
    "facets[status][]": "OP",           # Operating only
    "sort[0][column]": "nameplate-capacity-mw",
    "sort[0][direction]": "desc",
    "length": 5000,
}
if api_key:
    params["api_key"] = api_key
```

**Response parsing:** Records are in `response["response"]["data"]`. Key fields per row:
- `plantid` or `plantId` — plant identifier
- `plantName` or `plant_name` — plant name
- `nameplate-capacity-mw` — generator capacity in MW
- `energy_source_code` or `energySourceCode` — fuel code (e.g. "NG", "NUC", "SUN")
- `latitude`, `longitude` — plant coordinates
- `generatorid` or `generatorId` — individual generator ID
- `balancing_authority_code` or `balancingAuthorityCode` — BA code
- `county` — county name

**Aggregation:** API returns one row per generator. Aggregate by `plantid` to get plant-level totals:
```python
plants_by_id = {}
for row in rows:
    pid = str(row["plantid"])
    cap = float(row["nameplate-capacity-mw"])
    if pid not in plants_by_id:
        plants_by_id[pid] = {
            "plantid": pid,
            "name": row.get("plantName", ""),
            "total_mw": 0.0,
            "fuel_code": row.get("energy_source_code", "NG"),
            "lat": row.get("latitude"),
            "lon": row.get("longitude"),
            "ba": row.get("balancing_authority_code", ""),
            "generators": [],
        }
    plants_by_id[pid]["total_mw"] += cap
    plants_by_id[pid]["generators"].append({
        "genid": row.get("generatorid", ""),
        "cap_mw": cap,
        "fuel": row.get("energy_source_code", ""),
    })

# Filter small plants
plants = [p for p in plants_by_id.values() if p["total_mw"] >= 1.0]
```

**Output format** (cache as JSON array):
```json
[
  {
    "plantid": "6168",
    "name": "North Anna",
    "total_mw": 1960.4,
    "fuel_code": "NUC",
    "lat": 38.06,
    "lon": -77.79,
    "ba": "PJM",
    "generators": [{"genid": "1", "cap_mw": 979.7, "fuel": "NUC"}]
  }
]
```

**State abbreviations:** Use the state_codes.json resource for state name → abbreviation mapping.

## 3. OSM Power Infrastructure (Optional)

Downloads all power-tagged features for a US state. Useful as backup for plant matching when EIA data is incomplete.

**API endpoint:** `https://overpass-api.de/api/interpreter` (POST)

**Overpass query:**
```
[out:json][timeout:300];
area["ISO3166-2"="{iso_code}"]->.searchArea;
(
  node["power"](area.searchArea);
  way["power"](area.searchArea);
  relation["power"](area.searchArea);
);
out body;
>;
out skel qt;
```

Replace `{iso_code}` with the ISO 3166-2 code (e.g. `US-VA`). Use the state_codes.json resource for state name → ISO code mapping.

**Converting Overpass JSON to GeoJSON:**
1. First pass: collect all nodes into a `{node_id: (lon, lat)}` dict
2. Second pass: for each element with a `power` tag:
   - Nodes → Point geometry
   - Ways → LineString (or Polygon if closed with 4+ points)
   - Copy all OSM tags as properties

**Extracting plants from OSM GeoJSON:**
Filter features where `properties.power` is `"plant"` or `"generator"`. Key OSM property fields:
- `plant:output:electricity` or `generator:output:electricity` — capacity string like `"1960 MW"`, `"500 GW"`, `"800 kW"`
- `plant:source` or `generator:source` — fuel string like `"nuclear"`, `"gas"`, `"solar;wind"`

**Parsing capacity from OSM:**
```python
import re
cap_str = props.get("plant:output:electricity", "")
m = re.match(r"([\d.]+)\s*(MW|GW|kW)?", cap_str, re.IGNORECASE)
if m:
    cap_mw = float(m.group(1))
    unit = (m.group(2) or "MW").upper()
    if unit == "GW": cap_mw *= 1000
    elif unit == "KW": cap_mw /= 1000
```

**Getting coordinates from OSM features:**
- Point features: `geometry["coordinates"]` gives `[lon, lat]`
- Polygon/LineString features: compute centroid using `shapely.geometry.shape(geometry).centroid`

**Fuel normalization:** The `plant:source` field uses lowercase strings. Take the first value before `;` and strip whitespace.

**Rate limits:** Overpass has a fair-use policy. Large states may take 2-5 minutes. Cache results.

## Caching Strategy

All downloaded data should be cached to avoid repeated API calls. Check if cache file exists before making requests. Typical cache layout:
```
.cache/
  eia_demand/PJM_2024-07-15.json
  eia_plants/VA_plants.json
  osm/virginia.geojson
```

## Data From Datalake

If the data is already available in the datalake, use `search_data_lake_catalog` to find it instead of downloading. Any JSON file matching the formats above works — just load and use directly.
