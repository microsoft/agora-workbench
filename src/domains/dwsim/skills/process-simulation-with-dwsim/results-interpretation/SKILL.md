---
name: results-interpretation
parent_skill: process-simulation-with-dwsim
description: Extract, analyze, and present simulation results from a solved DWSIM flowsheet, including stream data, unit operation performance, and mass/energy balance closure.
states: [dwsim.flowsheet_solved, dwsim.results_available]
---

# Results Interpretation

Use this skill when the simulation has converged and you need to extract, analyze,
or present results to the user.

## Stream Results

Call `get_stream_results` with the `flowsheet` object and `stream_name` to read:

- **temperature**: stream temperature in Kelvin.
- **pressure**: stream pressure in Pascal.
- **total_molar_flow**: total molar flow in mol/s.
- **total_mass_flow**: total mass flow in kg/s.
- **vapor_fraction**: mole fraction in the vapor phase (0 = all liquid, 1 = all vapor).
- **phase_compositions**: nested dictionary with per-phase, per-compound mole
  and mass fractions.

Use this on every key stream (feeds, products, intermediates) to build a complete
picture of the process.

## Unit Operation Results

Call `get_unit_operation_results` with the `flowsheet` object and `unit_name` to read:

- **unit_type**: the DWSIM class name (e.g. Heater, Cooler, DistillationColumn).
- **duty**: heat duty or energy flow in Watts. Positive = heat added to the
  process (endothermic / heater); negative = heat removed (exothermic / cooler).
- **efficiency**: equipment efficiency if applicable (pumps, compressors).
- **details**: dictionary of all extracted numeric properties for the unit.

## Flowsheet Summary

Call `get_flowsheet_summary` with the `flowsheet` object to read:

- **convergence_status**: `"converged"` or `"errors"`.
- **object_list**: list of all flowsheet objects with tag, type, and error status.
  Check this for any object-level errors.
- **mass_balance**: aggregate mass in/out/difference in kg/s.
- **energy_balance**: aggregate energy in/out/difference in Watts.

## Mass and Energy Balance Closure

A well-converged simulation should satisfy:
- **Mass balance**: relative difference `|in - out| / in < 0.1%`. If the
  difference is larger, check for missing outlet streams, unconverged unit
  operations, or unbalanced reaction stoichiometry.
- **Energy balance**: relative difference should also be < 0.1%. Larger
  discrepancies may indicate missing energy stream connections or specification
  inconsistencies.

Red flags:
- Mass difference > 1% → likely a disconnected stream or unconverged unit.
- Negative total molar flow on any stream → mole fractions may not sum to 1.0.
- Extremely high or low temperatures (e.g. > 5000 K or < 50 K) → possible
  specification error or wrong property package.

## Reporting Template

When presenting results to the user, follow this structure:

### 1. Feed Conditions
For each feed stream: name, temperature, pressure, total flow, composition.

### 2. Unit Operation Summary
For each unit: name, type, key duty or performance metric.

### 3. Product Conditions
For each product stream: name, temperature, pressure, total flow, composition,
purity of target compound.

### 4. Overall Mass and Energy Balance
- Total mass in vs. out, relative difference.
- Total energy in vs. out, relative difference.
- Convergence status.

### 5. Key Observations
- Notable findings (e.g. high energy consumption, low purity, phase behavior).
- Recommendations for improvement if applicable.

See [references/reporting-checklist.md](references/reporting-checklist.md) for a
detailed checklist of what to report for each unit type.
