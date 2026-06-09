---
name: energysystems-pypsa
description: Power system modeling and analysis with PyPSA inside execute_energysystems_code — network setup, components, power flow, optimal dispatch, capacity expansion, cost and topology analysis. Covers PyPSA conventions and the gotchas that cause silently wrong results.
---

# Energy Systems / PyPSA

Use this skill when the user asks about power systems, electrical networks,
generators, transmission lines, power flow, optimal dispatch, capacity
planning, or renewable integration. Code runs in the
`execute_energysystems_code` tool with PyPSA auto-imported.

The domain ships eight tools whose parameters and return values are
described in their own schemas (visible through the tool interface). This
skill covers what the schemas don't: PyPSA conventions, the non-obvious
setup patterns, and the gotchas that produce silently wrong results.

## Domain Tools

```
define_network
  └── add_components
        ├── run_power_flow
        ├── run_optimal_power_flow ──► analyze_costs
        ├── analyze_topology
        └── add_time_series ──► run_capacity_expansion
```

The tools are plain Python functions inside `execute_energysystems_code`.
Write PyPSA directly when a task falls outside them.
`define_network` returns a live `pypsa.Network` object; pass that object to
the other tools in the same session.

## Local Data Catalog

This server ships ready-made power-grid datasets. Search for one with
`search_data` instead of building a network from scratch.

## Auto-Imported Modules

Available without explicit imports:

```python
import pypsa
import numpy as np
import pandas as pd
import networkx as nx
import matplotlib.pyplot as plt
```

## Critical: PyPSA Conventions

**Bus-based topology** — every component (generator, load, storage) attaches
to a bus; lines connect bus pairs. Define buses before anything that
references them, or the add silently fails.

**Snapshots** — time-series data must match the network's snapshot index
length. Check `len(n.snapshots)` before attaching a profile; a length
mismatch is the most common error.

**Per-unit system** — PyPSA uses per-unit for voltages (`v_mag_pu`),
capacity factors (`p_max_pu`), and state of charge (`state_of_charge`).
Physical units (MW, MVAr) are used for `p_nom`, `p_set`, `s_nom`.

## Component Parameters

`add_components` takes lists of dicts. The keys that matter per component:

| Component | Key Parameters |
|-----------|---------------|
| Bus | `v_nom` (kV) |
| Generator | `bus`, `p_nom` (MW), `marginal_cost` (€/MWh), `carrier`, `p_nom_extendable`, `capital_cost` |
| Load | `bus`, `p_set` (MW) |
| Line | `bus0`, `bus1`, `s_nom` (MVA), `x` (pu), `r` (pu) |
| StorageUnit | `bus`, `p_nom` (MW), `max_hours`, `marginal_cost`, `capital_cost`, `p_nom_extendable` |

Tag generators with `carrier` (`"coal"`, `"gas"`, `"wind"`, `"solar"`) so
`analyze_costs` can break costs down by technology.

## Capacity Expansion: Extendable Components

To let the optimizer size a component, start it at zero capacity and mark it
extendable with an annualized capital cost:

```python
network = define_network(name="grid", snapshots=24)
add_components(
    network=network,
    generators=[{
        "name": "Wind", "bus": "Bus0", "carrier": "wind",
        "p_nom": 0,                # start with no capacity
        "p_nom_extendable": True,  # let the optimizer size it
        "capital_cost": 1000,      # €/MW/year annualized
        "marginal_cost": 0,
    }],
)
```

Then attach time-varying profiles with `add_time_series` (each profile dict:
`component_type`, `component_name`, `attribute`, `values`) before
`run_capacity_expansion`. Common time-varying attributes:

| Component | Attribute | Meaning |
|-----------|-----------|---------|
| Generator | `p_max_pu` | Capacity factor 0–1 (wind/solar profiles) |
| Load | `p_set` | Absolute demand (MW) per snapshot |
| StorageUnit | `inflow` | Natural inflow (MW), e.g. hydro |

## Power Flow: AC vs DC

| Method | Use Case | Speed | Accuracy |
|--------|----------|-------|----------|
| `"ac"` | Voltage / reactive-power analysis | Slower | Full non-linear |
| `"dc"` | Quick feasibility, large networks | Fast | Linear approximation |

## Solver

HiGHS is pre-installed and the default for optimization (OPF and capacity
expansion) — no license required:

```python
status, _ = n.optimize(solver_name="highs")
```

## Gotchas

- Generators need `marginal_cost` for OPF to be meaningful — one without it
  is treated as free and dispatched first.
- Lines need `s_nom` > 0 for line-loading calculations.
- Profile `values` length must exactly equal `len(network.snapshots)`.
- Use a full year of snapshots (8760) for realistic capacity-expansion
  results; a 24-hour horizon is fine for quick checks.

## End-to-End Example

```python
# Build a 2-bus network, solve OPF, break down costs by carrier
network = define_network(name="grid", snapshots=24)
add_components(
    network=network,
    buses=[{"name": "North"}, {"name": "South"}],
    generators=[
        {"name": "Gas", "bus": "North", "p_nom": 500, "marginal_cost": 50, "carrier": "gas"},
        {"name": "Wind", "bus": "South", "p_nom": 300, "marginal_cost": 0, "carrier": "wind"},
    ],
    loads=[{"name": "City", "bus": "South", "p_set": 400}],
    lines=[{"name": "N-S", "bus0": "North", "bus1": "South", "s_nom": 500, "x": 0.01}],
)
opf = run_optimal_power_flow(network=network)
costs = analyze_costs(network=network)
print(opf["status"], costs["total_cost"], costs["cost_by_carrier"])
```
