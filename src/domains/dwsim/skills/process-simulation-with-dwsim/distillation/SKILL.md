---
name: distillation
description: Design and configure rigorous distillation columns (single-feed, multi-feed/extractive, and absorption/stripping) for separating mixtures.
parent_skill: process-simulation-with-dwsim
states: [dwsim.flowsheet_exists, dwsim.flowsheet_solved]
---

# Distillation

Use this skill when the user needs to separate a liquid mixture by boiling-point
differences using a distillation column, or asks about column design, reflux
ratios, or stage counts.

## Column Specification

Call `add_distillation_column` with:
- `flowsheet`: the flowsheet object.
- `name`: e.g. `"COLUMN-1"`.
- `feed_stream_name`: tag of the feed stream (must already exist with defined composition).
- `feed_stage`: tray number for the feed (1-based from top; stage 1 = condenser).
- `num_stages`: total stages including condenser (stage 1) and reboiler (last stage).
- `condenser_type`: `"TotalCondenser"` or `"PartialCondenser"`.
- `distillate_stream_name`: tag for the overhead product stream.
- `bottoms_stream_name`: tag for the bottoms product stream.
- `reflux_ratio`: L/D ratio (liquid returned to column / distillate withdrawn).
- `reboiler_duty`: initial guess for reboiler heat input in Watts.

## Feed Stage Heuristics

The feed should enter at the stage where the column composition most closely
matches the feed composition. Rules of thumb:
- For a binary mixture, place the feed at roughly **1/3 to 1/2** of the way down
  from the top (e.g. stage 5–7 for a 15-stage column).
- Lighter feeds (higher vapor fraction) should enter lower; heavier feeds enter higher.
- The Kirkbride correlation provides a more rigorous estimate — see
  [references/column-design-heuristics.md](references/column-design-heuristics.md).

## Reflux Ratio Guidance

- Start with a reflux ratio of **1.2–1.5× the minimum reflux** (R_min).
- R_min can be estimated from the Underwood equations for binary or
  multi-component systems.
- A higher reflux ratio improves separation but increases energy consumption.
- If the column fails to converge, try **increasing the reflux ratio** and
  **increasing the number of stages** simultaneously.

## Condenser Type Selection

- **TotalCondenser**: all overhead vapor is condensed to liquid. Use when you want
  a liquid distillate product (most common case).
- **PartialCondenser**: only part of the overhead is condensed; the remainder exits
  as vapor. Use when you need a vapor overhead product (e.g. gas recovery columns).

## Initial Reboiler Duty Guess

A rough estimate for the reboiler duty:

    Q_reb ≈ (R + 1) × D × ΔH_vap

where R is the reflux ratio, D is the distillate molar flow (mol/s), and ΔH_vap
is the average heat of vaporization of the mixture (~30–40 kJ/mol for many
organics). Convert to Watts (1 kJ/s = 1000 W). Start with a moderate guess
(e.g. 1e6 W) and let the solver adjust.

## Convergence Tips

1. **Start simple**: use fewer stages (e.g. 10) and a moderate reflux ratio to
   get an initial converged solution, then increase stages gradually.
2. **Relax specifications**: if the column fails, try widening the reflux ratio
   or increasing the reboiler duty guess.
3. **Check the feed**: ensure the feed stream is properly defined with valid
   composition, temperature, and pressure before adding the column.
4. **Verify outlet names**: distillate and bottoms stream names must not conflict
   with existing streams.

See [references/column-design-heuristics.md](references/column-design-heuristics.md)
for shortcut design methods and typical parameters.

## Multi-Feed Distillation (Extractive Distillation)

Call `add_multi_feed_distillation_column` when a column requires multiple feed
streams entering at different stages — most commonly for extractive distillation
where a main feed and a solvent feed enter at different trays.

Parameters:
- `flowsheet`: the flowsheet object.
- `name`: e.g. `"ED-COL-1"`.
- `feeds_json`: JSON list of feed specifications:
  ```json
  [
    {"stream_name": "FEED", "stage": 15},
    {"stream_name": "SOLVENT", "stage": 3}
  ]
  ```
  Stage is 1-based (1 = condenser). The solvent typically enters near the top,
  and the main feed enters in the middle or lower section.
- `num_stages`: total stages including condenser and reboiler (can exceed 12).
- `condenser_type`: `"TotalCondenser"` or `"PartialCondenser"`.
- `distillate_stream_name`: overhead product stream tag.
- `bottoms_stream_name`: bottoms product stream tag.
- `reflux_ratio`: L/D ratio.
- `bottoms_rate` (optional): reboiler spec value — interpretation depends on `reboiler_spec_type` (molar flow in mol/s for the default, duty in W for Heat_Duty, etc.).
- `reboiler_spec_type` (optional): `"Product_Molar_Flow_Rate"` (default), `"Product_Mass_Flow_Rate"`, `"Heat_Duty"`, `"Component_Molar_Flow_Rate"`, `"Component_Fraction"`, or `"Temperature"`.
- `condenser_pressure` (optional): condenser pressure in Pa.
- `reboiler_pressure` (optional): reboiler pressure in Pa.

### Extractive Distillation Tips

1. **Trace components in all feeds**: for ternary extractive distillation systems,
   both the main feed and solvent feed must contain trace amounts (~0.2–1 mol%)
   of **all** components. Zero mole fractions in a feed cause the column solver
   initialization to fail.
2. **Solvent-to-feed ratio**: typical solvent-to-feed molar ratios are 1:1 to 5:1
   depending on the system. Start with 2:1 and adjust.
3. **Stage count**: extractive distillation typically needs 20–40 stages.
   Start with 25 and refine.
4. **Peng-Robinson** is generally the most robust property package for ED column
   convergence, even for polar systems.

## Absorption Column

Call `add_absorption_column` for gas scrubbing, natural gas drying, CO₂ capture,
or any contacting operation where gas and liquid flow counter-currently without
a condenser or reboiler.

Parameters:
- `flowsheet`: the flowsheet object.
- `name`: e.g. `"ABS-1"`.
- `num_stages`: number of theoretical stages (minimum 4).
- `gas_inlet_name`: tag of the gas feed stream (enters at the **bottom**).
- `liquid_inlet_name`: tag of the liquid solvent stream (enters at the **top**).
- `gas_outlet_name`: tag for the treated gas outlet (exits at the **top**).
- `liquid_outlet_name`: tag for the rich solvent outlet (exits at the **bottom**).
- `operating_pressure` (optional): column pressure in Pa (0 to use feed pressure).

### Absorption Column Tips

1. **Minimum 4 stages**: DWSIM requires at least 4 stages for absorption columns.
   Typical industrial absorbers use 6–20 stages.
2. **Solvent selection**: the solvent should have high selectivity for the target
   component (e.g. TEG for water removal, cold methanol for CO₂ in Rectisol).
3. **Pressure**: higher pressure generally improves absorption but may cause solver
   difficulties above ~15 bar for glycol-based solvents with Peng-Robinson.
4. **Temperature**: lower solvent temperature improves absorption capacity.
   Pre-cool the solvent if possible.
