---
name: network-modeling
description: Build power system models with PyPSA — define networks with time snapshots, add buses, generators, loads, transmission lines, and storage units.
states:
  - energysystems.network_defined
  - energysystems.components_added
---

# Network Modeling

Use this skill when the user wants to create a power system model,
define a network topology, or add generation, load, and transmission
components.

## State Graph

```
define_network(name, snapshots, start, freq)
    → energysystems.network_defined

add_components(network_name, buses, generators, loads, lines, storage_units)
    requires: energysystems.network_defined
    → energysystems.components_added
```

## Tools

### define_network

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `name` | str | Yes | — | Network name |
| `snapshots` | int | No | 24 | Number of hourly time steps |
| `start` | str | No | `"2025-01-01"` | Start datetime (ISO format) |
| `freq` | str | No | `"h"` | Pandas frequency string |

**Returns:** `name`, `num_snapshots`, `frequency`, `start`, `end`

### add_components

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `network_name` | str | Yes | — | Network name from define_network |
| `buses` | list | No | — | Bus dicts with `name`, `v_nom` |
| `generators` | list | No | — | Generator dicts with `name`, `bus`, `p_nom`, `marginal_cost` |
| `loads` | list | No | — | Load dicts with `name`, `bus`, `p_set` |
| `lines` | list | No | — | Line dicts with `name`, `bus0`, `bus1`, `s_nom`, `x` |
| `storage_units` | list | No | — | Storage dicts with `name`, `bus`, `p_nom`, `max_hours` |

**Returns:** `num_buses`, `num_generators`, `num_loads`, `num_lines`,
`num_storage_units`, `summary`

## Workflow Example

```python
# Step 1: Create a network with 24 hourly snapshots
net = define_network(name="simple_grid", snapshots=24)

# Step 2: Add a 3-bus system
result = add_components(
    network_name="simple_grid",
    buses=[
        {"name": "Bus0", "v_nom": 110},
        {"name": "Bus1", "v_nom": 110},
        {"name": "Bus2", "v_nom": 110},
    ],
    generators=[
        {"name": "Coal", "bus": "Bus0", "p_nom": 200, "marginal_cost": 30, "carrier": "coal"},
        {"name": "Wind", "bus": "Bus1", "p_nom": 150, "marginal_cost": 0, "carrier": "wind"},
    ],
    loads=[
        {"name": "City", "bus": "Bus2", "p_set": 100},
    ],
    lines=[
        {"name": "Line01", "bus0": "Bus0", "bus1": "Bus1", "s_nom": 200, "x": 0.01},
        {"name": "Line12", "bus0": "Bus1", "bus1": "Bus2", "s_nom": 200, "x": 0.01},
    ],
)
print(result["summary"])
```

## Notes

- Always define buses before referencing them in generators, loads, or lines.
- Use `carrier` to tag generators by fuel type (e.g., "coal", "gas", "wind", "solar")
  for cost analysis breakdown.
- For OPF, set `marginal_cost` on generators. For capacity expansion, also set
  `p_nom_extendable=True` and `capital_cost`.
