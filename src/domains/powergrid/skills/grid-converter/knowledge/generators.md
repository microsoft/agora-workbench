# Generator Identification and Matching

## Step 1: Identify Generator Buses

Generator buses are identified from the xlsx in priority order:

1. **Name pattern**: Bus names containing `_GEN`, `GEN_`, or ending with `GEN` (case-insensitive)
2. **LSC supplement**: If fewer than ~20 generators found, add buses with Load Serving Capacity ≥ 200 MW (up to 30 extra), sorted by highest LSC first
3. **External tie buses**: Buses referenced by lines but missing from the bus table, and NOT flagged as generators, become large equivalent generators at the network boundary

## Step 2: Match to Real Plant Data

Match each generator bus to real plant data by geographic proximity (nearest unused match within radius).

### Priority 1 — EIA Plants (most authoritative)

Source: EIA-860 data downloaded via API (see data_fetching.md).

**Matching algorithm:**
```python
def match_bus_to_plant(bus_lon, bus_lat, plants, radius_km=10.0):
    best, best_dist = None, radius_km
    for p in plants:
        if p["used"] or p.get("lat") is None:
            continue
        d = haversine_km(bus_lon, bus_lat, p["lon"], p["lat"])
        if d < best_dist:
            best_dist, best = d, p
    if best:
        best["used"] = True  # prevent double-matching
    return best
```

**Fuel code mapping** (EIA code → canonical fuel, marginal cost $/MWh):

| EIA Code | Fuel | Cost |
|----------|------|------|
| NG | gas | 35 |
| NUC | nuclear | 8 |
| BIT, SUB, LIG | coal | 25 |
| WAT | hydro | 8 |
| SUN | solar | 0 |
| WND | wind | 0 |
| DFO, RFO | oil | 180 |
| WDS, BLQ | biomass | 45 |
| MSW, LFG, OBG | waste | 50 |
| MWH | battery | 5 |
| GEO | geothermal | 5 |

### Priority 2 — OSM Plants (backup)

Source: OSM GeoJSON downloaded via Overpass API (see data_fetching.md).

Filter features where `properties.power` is `"plant"` or `"generator"`. Extract:
- **Capacity**: Parse `plant:output:electricity` (e.g. `"1960 MW"`) — handle MW/GW/kW units. Skip plants with capacity ≤ 0.
- **Fuel**: Use `plant:source` (e.g. `"nuclear"`, `"gas"`). Take first value before `;`, lowercase.
- **Coordinates**: For Point features use coordinates directly. For Polygon/LineString use `shapely.geometry.shape(geom).centroid`.

Same matching algorithm: nearest unused OSM plant within 10 km.

Fuel cost lookup for OSM fuel strings:

| OSM Fuel | Cost ($/MWh) |
|----------|-------------|
| nuclear | 8 |
| hydro, water | 8 |
| solar | 0 |
| wind | 0 |
| coal | 25 |
| gas, natural_gas | 35 |
| oil | 180 |
| biomass, wood | 45 |
| waste | 50 |
| battery | 5 |

### Priority 3 — Default

If no EIA or OSM match: 200 MW gas at $35/MWh.

## Step 3: External Tie Generators

External tie buses get special treatment — they represent the rest of the ISO:
- **Capacity**: 5,000 MW
- **Marginal cost**: $30/MWh (ISO average LMP)
- **p_min_pu**: 0.0 (can dispatch down to zero)
- **carrier**: "gas"

## Minimum Stable Output (p_min_pu)

| Fuel | p_min_pu |
|------|----------|
| nuclear | 0.50 |
| coal | 0.30 |
| gas (combined cycle) | 0.30 |
| gas_turbine | 0.0 |
| oil | 0.0 |
| hydro | 0.0 |
| solar | 0.0 |
| wind | 0.0 |
| biomass | 0.30 |
| waste | 0.30 |
| battery | 0.0 |
| geothermal | 0.50 |

## Slack Bus

The highest-kV generator bus becomes the slack bus (`control="Slack"`). All other generators get `control="PV"`.

## Adding Generators to PyPSA

```python
n.add("Generator", gen_name,
    bus=f"bus_{bid}",
    p_nom=capacity_mw,
    p_min_pu=p_min_pu,
    p_max_pu=1.0,
    marginal_cost=cost,
    carrier=fuel,
    control="Slack" if bid == slack_bus else "PV",
)
```
