---
name: energysystems-pypsa
description: Power system modeling and analysis using PyPSA — network definition, component management, power flow, optimal dispatch, capacity expansion, cost analysis, and topology via domain tools and the execute_energysystems_code tool.
states:
  - energysystems.network_defined
  - energysystems.components_added
  - energysystems.time_series_attached
  - energysystems.power_flow_solved
  - energysystems.opf_solved
  - energysystems.capacity_expansion_solved
  - energysystems.costs_analyzed
  - energysystems.topology_analyzed
---

# Energy Systems / PyPSA

Use this skill when the user asks about power systems, electrical networks,
generators, transmission lines, optimal power flow, capacity planning,
renewable integration, or any power system analysis task. Code runs in the
`execute_energysystems_code` tool with PyPSA auto-imported.

## State Graph Overview

The domain tools form a directed graph of workflows. `define_network` is the
entry point; downstream tools have prerequisite states that guide workflow
planning.

```
define_network
  → energysystems.network_defined
        │
        └── add_components → energysystems.components_added
                │
                ├── run_power_flow → energysystems.power_flow_solved
                │
                ├── run_optimal_power_flow → energysystems.opf_solved
                │       │
                │       ├── analyze_costs → energysystems.costs_analyzed
                │       │
                │       └── (also feeds run_capacity_expansion if extendable)
                │
                ├── analyze_topology → energysystems.topology_analyzed
                │
                └── add_time_series → energysystems.time_series_attached
                        │
                        └── run_capacity_expansion → energysystems.capacity_expansion_solved
```

## Workflow Skills

| Skill | Tools | Description |
|-------|-------|-------------|
| [network-modeling](network-modeling.md) | `define_network` → `add_components` | Build power system models |
| [power-flow-analysis](power-flow-analysis.md) | `run_power_flow` → `run_optimal_power_flow` → `analyze_costs` | Dispatch and pricing |
| [capacity-planning](capacity-planning.md) | `add_time_series` → `run_capacity_expansion` | Investment optimization |

## Auto-Imported Modules

These are available without explicit imports:

```python
import pypsa
import numpy as np
import pandas as pd
import networkx as nx
import matplotlib.pyplot as plt
```

## Critical: PyPSA Conventions

**Bus-based topology** — All components (generators, loads, storage) connect
to buses. Lines connect bus pairs. Always define buses first.

```python
# CORRECT order
n.add("Bus", "Bus0", v_nom=110)
n.add("Generator", "Gen0", bus="Bus0", p_nom=100)

# WRONG — generator references non-existent bus
n.add("Generator", "Gen0", bus="Bus0", p_nom=100)  # Bus0 not defined yet
```

**Snapshots** — Time-series data must match the network's snapshot index
length. Always check `len(n.snapshots)` before attaching profiles.

**Per-unit system** — PyPSA uses per-unit for voltages (`v_mag_pu`),
capacity factors (`p_max_pu`), and state of charge (`state_of_charge`).
Physical units (MW, MVAr) are used for `p_nom`, `p_set`, `s_nom`.

## Component Parameters Quick Reference

| Component | Key Parameters |
|-----------|---------------|
| Bus | `v_nom` (kV) |
| Generator | `bus`, `p_nom` (MW), `marginal_cost` (€/MWh), `carrier`, `p_nom_extendable`, `capital_cost` |
| Load | `bus`, `p_set` (MW) |
| Line | `bus0`, `bus1`, `s_nom` (MVA), `x` (pu), `r` (pu) |
| StorageUnit | `bus`, `p_nom` (MW), `max_hours`, `marginal_cost`, `capital_cost`, `p_nom_extendable` |

## Solver

The HiGHS solver is pre-installed and used by default for optimization
(OPF and capacity expansion). No license required.

```python
status, _ = n.optimize(solver_name="highs")
```
