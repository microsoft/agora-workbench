# Demand Allocation

## Step 1: Determine Total Demand

### From EIA-930 data (preferred)

Load the cached EIA-930 JSON (see data_fetching.md) and look up the target hour:

```python
total_demand = None
for rec in eia_data["records"]:
    if rec["hour"] == target_hour:
        total_demand = rec["demand_mw"] * region_share
        break
```

- `region_share`: this region's fraction of the ISO. Example: DVP ≈ 17% of PJM (`0.17`).
- `target_hour`: typically 16 (4 PM peak).

### Fallback (no EIA data)

```python
total_demand = total_generation_capacity_mw * 0.85
```

### Feasibility cap

Demand must not exceed generation capacity:

```python
total_demand = min(total_demand, total_generation_capacity_mw * 0.95)
```

## Step 2: Distribute Across Buses

Only buses at or below **230 kV** receive load. Buses at 345 kV, 500 kV, 765 kV are pure transmission — skip them.

### LSC-weighted allocation

**90% of demand** → distributed proportionally to Load Serving Capacity (LSC):

```python
lsc_share = total_demand * 0.9
total_lsc = sum(b["load_serving_capacity_mw"] for b in eligible_buses.values())
lsc_scale = lsc_share / total_lsc if total_lsc > 0 else 0
# Each bus with LSC > 0 gets: load = lsc * lsc_scale
```

**10% of demand** → split evenly among buses with zero LSC:

```python
non_lsc_share = total_demand * 0.1
non_lsc_count = sum(1 for b in eligible_buses.values()
                    if b["load_serving_capacity_mw"] <= 0)
non_lsc_each = non_lsc_share / max(1, non_lsc_count)
```

## Reactive Power

Each load also gets reactive power based on voltage level (higher kV = better power factor):

| Bus kV | Q/P Ratio | Approx Power Factor |
|--------|-----------|---------------------|
| 69 | 0.328 | 0.95 |
| 115 | 0.328 | 0.95 |
| 138 | 0.250 | 0.97 |
| 161 | 0.250 | 0.97 |
| 230 | 0.203 | 0.98 |

```python
q_set = p_set * qp_ratio_for_kv
```

## Adding Loads to PyPSA

```python
n.add("Load", load_name,
    bus=f"bus_{bid}",
    p_set=p_mw,
    q_set=q_mvar,
)
```

Only add loads with `p_set > 0.01` MW.

## Multi-Zone Considerations

For multi-ISO studies, demand should be allocated per zone:
1. Compute total demand for each zone separately (different ISO shares, different EIA data)
2. Distribute within each zone using that zone's buses' LSC
3. Cap each zone's demand against its generation capacity
