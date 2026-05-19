---
name: capacity-planning
description: Attach time-varying profiles and run investment optimization to determine optimal generation and storage capacity additions using PyPSA.
states:
  - energysystems.components_added
  - energysystems.time_series_attached
  - energysystems.capacity_expansion_solved
---

# Capacity Planning

Use this skill when the user wants to model time-varying demand or
renewable generation, optimize investment in new capacity, or evaluate
scenarios for renewable integration.

## State Graph

```
add_components(...)
    → energysystems.components_added

add_time_series(network_name, profiles)
    requires: energysystems.components_added
    → energysystems.time_series_attached

run_capacity_expansion(network_name)
    requires: energysystems.time_series_attached
    → energysystems.capacity_expansion_solved
```

## Tools

### add_time_series

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `network_name` | str | Yes | — | Network name |
| `profiles` | list | Yes | — | List of profile dicts (see below) |

Each profile dict:

| Key | Type | Description |
|-----|------|-------------|
| `component_type` | str | `"generators"`, `"loads"`, `"storage_units"` |
| `component_name` | str | Name of existing component |
| `attribute` | str | Time-varying attribute (e.g. `"p_max_pu"`, `"p_set"`) |
| `values` | list | Numeric values, one per snapshot |

**Returns:** `num_profiles_attached`, `snapshot_count`, `components`

### run_capacity_expansion

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `network_name` | str | Yes | — | Network name |

**Returns:** `status`, `total_system_cost`, `optimal_capacities`,
`investment_by_type`

## Extendable Components

To make a component eligible for capacity expansion, set these parameters
when adding it:

```python
add_components(
    network_name="grid",
    generators=[{
        "name": "Wind",
        "bus": "Bus0",
        "carrier": "wind",
        "p_nom": 0,              # Start with no capacity
        "p_nom_extendable": True, # Allow optimizer to size it
        "capital_cost": 1000,     # €/MW/year annualized
        "marginal_cost": 0,       # No fuel cost
    }],
)
```

## Common Profile Attributes

| Component | Attribute | Description |
|-----------|-----------|-------------|
| Generator | `p_max_pu` | Capacity factor (0–1), e.g. wind/solar profiles |
| Load | `p_set` | Absolute demand (MW) per snapshot |
| StorageUnit | `inflow` | Natural inflow (MW), e.g. hydro |

## Workflow Example

```python
import numpy as np

# Step 1: Attach wind capacity factors (24h profile)
wind_cf = [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9,
           0.8, 0.6, 0.4, 0.3, 0.2, 0.3, 0.5, 0.7,
           0.8, 0.7, 0.5, 0.3, 0.2, 0.2, 0.1, 0.1]

# Step 2: Attach load profile (varying demand)
load_profile = [80, 75, 70, 65, 70, 80, 100, 120,
                130, 125, 120, 115, 110, 115, 120, 125,
                130, 135, 130, 120, 110, 100, 90, 85]

ts = add_time_series(
    network_name="grid",
    profiles=[
        {"component_type": "generators", "component_name": "Wind",
         "attribute": "p_max_pu", "values": wind_cf},
        {"component_type": "loads", "component_name": "City",
         "attribute": "p_set", "values": load_profile},
    ],
)

# Step 3: Run capacity expansion
result = run_capacity_expansion(network_name="grid")
print(f"Status: {result['status']}")
print(f"Total cost: {result['total_system_cost']}")
for cap in result["optimal_capacities"]:
    print(f"  {cap['component']}: {cap['p_nom_opt_mw']} MW")
```

## Notes

- Profile `values` length must exactly match `num_snapshots` from
  `define_network`.
- The optimizer minimizes total annualized cost = investment cost +
  operational cost over the snapshot period.
- Use longer snapshot periods (e.g., 8760 for a full year) for
  realistic capacity expansion results.
