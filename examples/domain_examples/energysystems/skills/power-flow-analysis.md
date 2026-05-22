---
name: power-flow-analysis
description: Run AC/DC power flow and optimal power flow on PyPSA networks — compute bus voltages, line loading, economic dispatch, marginal prices, and cost breakdowns.
states:
  - energysystems.components_added
  - energysystems.power_flow_solved
  - energysystems.opf_solved
  - energysystems.costs_analyzed
---

# Power Flow Analysis

Use this skill when the user wants to run power flow, optimize dispatch,
analyze marginal pricing, or break down generation costs.

## State Graph

```
add_components(...)
    → energysystems.components_added

run_power_flow(network_name, method)
    requires: energysystems.components_added
    → energysystems.power_flow_solved

run_optimal_power_flow(network_name)
    requires: energysystems.components_added
    → energysystems.opf_solved

analyze_costs(network_name)
    requires: energysystems.opf_solved
    → energysystems.costs_analyzed
```

## Tools

### run_power_flow

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `network_name` | str | Yes | — | Network name |
| `method` | str | No | `"ac"` | `"ac"` (Newton-Raphson) or `"dc"` (linear) |

**Returns:** `converged`, `method`, `bus_results` (voltage, angle, power),
`line_loading` (loading % and flows)

### run_optimal_power_flow

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `network_name` | str | Yes | — | Network name |

**Returns:** `status`, `objective_value`, `generator_dispatch`,
`line_flows`, `marginal_prices`

### analyze_costs

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `network_name` | str | Yes | — | Network name (OPF must be solved) |

**Returns:** `total_cost`, `cost_by_carrier`, `marginal_price_stats`,
`most_expensive_bus`

## AC vs DC Power Flow

| Method | Use Case | Speed | Accuracy |
|--------|----------|-------|----------|
| AC (`"ac"`) | Voltage analysis, reactive power | Slower | Full non-linear |
| DC (`"dc"`) | Quick feasibility check, large networks | Fast | Linear approximation |

## Workflow Example

```python
# Step 1: Run DC power flow (quick check)
pf = run_power_flow(network_name="simple_grid", method="dc")
print(f"Converged: {pf['converged']}")
for bus in pf["bus_results"]:
    print(f"  {bus['bus']}: P={bus.get('p', 'N/A')} MW")

# Step 2: Run optimal power flow (minimize cost)
opf = run_optimal_power_flow(network_name="simple_grid")
print(f"Status: {opf['status']}, Total cost: {opf['objective_value']}")
for gen in opf["generator_dispatch"]:
    print(f"  {gen['generator']}: {gen['p_mean_mw']} MW avg")

# Step 3: Analyze cost breakdown
costs = analyze_costs(network_name="simple_grid")
print(f"Total: {costs['total_cost']}")
for carrier, cost in costs["cost_by_carrier"].items():
    print(f"  {carrier}: {cost}")
```

## Notes

- Generators must have `marginal_cost` set for OPF to produce meaningful
  results. Generators without costs are treated as free.
- Lines must have `s_nom` > 0 for line loading calculations.
- Marginal prices are available per bus per snapshot after OPF.
