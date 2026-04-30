---
name: sensitivity-and-optimization
parent_skill: process-simulation-with-dwsim
description: Run parametric sweeps and numerical optimization on DWSIM flowsheets to explore design space and find optimal operating conditions.
states: [dwsim.flowsheet_solved, dwsim.optimization_complete]
---

# Sensitivity Analysis and Optimization

Use this skill when the user asks "what if", wants to sweep a parameter across a
range, or optimize a design variable to minimize or maximize an objective.

## Sensitivity Analysis

Call `run_sensitivity_analysis` to sweep one variable and observe how an objective
responds:

- `flowsheet`: the flowsheet object (should already be solved at a base case).
- `variable_object`: tag of the flowsheet object whose property is varied
  (e.g. `"HTR-1"` for a heater).
- `variable_property`: DWSIM property code (PROP\_\* format) to vary
  (e.g. `"PROP_HT_2"` for heater outlet temperature).
- `min_value`: lower bound for the sweep.
- `max_value`: upper bound for the sweep.
- `num_points`: number of evenly spaced points (including endpoints).
- `objective_object`: tag of the object from which the objective is read
  (e.g. `"PRODUCT"` stream).
- `objective_property`: DWSIM property code to read as the objective
  (e.g. `"PROP_MS_0"` for stream temperature).

The tool returns parallel arrays `variable_values` and `objective_values`.

### Example: Sweep Heater Outlet Temperature

Observe how product stream temperature changes as the heater setpoint varies:

1. Solve the base-case flowsheet first.
2. `run_sensitivity_analysis` with:
   - `variable_object: "HTR-1"`
   - `variable_property: "PROP_HT_2"`
   - `min_value: 350`, `max_value: 500`, `num_points: 16`
   - `objective_object: "PRODUCT"`
   - `objective_property: "PROP_MS_0"`

## PROP\_ Code Reference

DWSIM uses property codes (PROP\_\* strings) to identify specific properties of
flowsheet objects. See [references/prop-codes.md](references/prop-codes.md) for
a table of the most commonly used codes.

## Optimization

Call `run_optimization` to find the best values for one or more decision variables:

- `flowsheet`: the flowsheet object.
- `objective_object`: tag of the object providing the objective value.
- `objective_property`: DWSIM property code of the objective.
- `minimize`: `true` to minimize, `false` to maximize.
- `variables`: JSON list of decision-variable specifications:

```json
[
  {
    "object": "HTR-1",
    "property": "PROP_HT_2",
    "min": 350,
    "max": 500,
    "initial": 400
  }
]
```

- `constraints`: JSON list of inequality constraints (value ≥ 0 convention):

```json
[
  {
    "object": "PRODUCT",
    "property": "PROP_MS_0",
    "type": ">=",
    "value": 300
  }
]
```

Pass `"[]"` if there are no constraints.

### Example: Minimize Heater Duty Subject to Product Temperature

1. Solve the base case.
2. `run_optimization` with:
   - `objective_object: "HTR-1"`, `objective_property: "PROP_HT_3"` (heater duty)
   - `minimize: true`
   - `variables: '[{"object": "HTR-1", "property": "PROP_HT_2", "min": 350, "max": 500, "initial": 400}]'`
   - `constraints: '[{"object": "PRODUCT", "property": "PROP_MS_0", "type": ">=", "value": 350}]'`

## Best Practices

1. **Solve the base case first** — always have a converged base case before
   running sensitivity or optimization. Sweep failures often come from an
   unconverged starting point.
2. **Use sensitivity before optimization** — sweep the landscape to understand
   monotonicity, identify feasible ranges, and spot discontinuities.
3. **Keep variable count low** — Nelder-Mead works best with 1–3 variables.
   More variables require many more iterations.
4. **Use reasonable bounds** — tight bounds help the optimizer converge faster
   and avoid physically infeasible regions.
