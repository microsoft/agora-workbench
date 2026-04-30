---
name: troubleshooting
parent_skill: process-simulation-with-dwsim
description: Diagnose and fix simulation convergence failures, specification errors, and physically unreasonable results in DWSIM flowsheets.
states: [dwsim.flowsheet_exists, dwsim.flowsheet_solved]
---

# Troubleshooting

Use this skill when `solve_flowsheet` reports errors, a unit operation fails to
converge, or simulation results look physically unreasonable.

## Convergence Failures

After calling `solve_flowsheet`, check the response:
- If `converged` is `false`, read `error_messages` for per-object diagnostics.
- Call `get_flowsheet_summary` to see which specific objects have errors.

Common causes of convergence failure:
- **Missing stream connection**: a unit operation inlet or outlet is not connected
  to a defined stream.
- **Infeasible specification**: e.g. cooling a stream below its freezing point,
  or specifying a pressure lower than the outlet of a downstream unit.
- **Bad initial guess**: especially for distillation columns — reboiler duty or
  reflux ratio too far from the actual solution.

## Incremental Building Strategy

The most reliable approach is to build the flowsheet incrementally:
1. Create the flowsheet and add feed streams.
2. Add **one unit operation** at a time.
3. Call `solve_flowsheet` after each addition.
4. Verify results with `get_stream_results` before adding the next unit.
5. If a unit fails, fix it before proceeding.

This isolates problems to the most recently added unit, making diagnosis much
easier than debugging an entire flowsheet at once.

## Common Error Patterns

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| "Negative molar flow" in a stream | Mole fractions don't sum to 1.0 | Recalculate `compound_mole_fractions` to sum exactly to 1.0 |
| "Flash calculation failed" | T/P outside applicable range for the property package | Check that T and P are physically reasonable; try a different property package |
| Distillation column won't converge | Reflux ratio too low or reboiler duty guess too far off | Increase reflux ratio; start with more stages and a larger duty guess |
| Reactor gives zero conversion | `base_compound` not present in feed | Verify compound names match exactly between feed and reaction set |
| Heat exchanger temperature cross | Hot outlet colder than cold inlet | Increase hot outlet temperature or reduce cold inlet temperature |
| "Object not found" error | Misspelled stream or unit name | Use exact names (case-sensitive) as returned by creation tools |
| Pump fails on two-phase feed | Feed is partially vaporized | Cool the feed below its bubble point before pumping, or use a compressor for vapor |
| Very large mass balance error | Unbalanced reaction stoichiometry | Re-check stoichiometric coefficients; ensure atom balance |

## Property Package Mismatches

Choosing the wrong property package can cause subtle errors:
- **Peng-Robinson for highly polar liquids** (e.g. water-methanol): poor liquid
  activity prediction → incorrect phase splits. Switch to NRTL or UNIQUAC.
- **NRTL for high-pressure gas systems**: NRTL is designed for liquid-phase
  non-ideality. For gas-phase-dominated systems, use Peng-Robinson or SRK.
- **Steam Tables for mixtures**: Steam Tables only handle pure water. Any
  additional compound causes errors. Use NRTL or Peng-Robinson for water mixtures.
- **Raoult's Law for non-ideal systems**: Raoult's Law assumes ideal behavior.
  For any system with hydrogen bonding or significant polarity differences, use
  NRTL, UNIQUAC, or UNIFAC.

## Physically Unreasonable Results

Even if the solver reports convergence, sanity-check the results:
- **Temperature**: should be within a physically plausible range for the process
  (typically 200–1500 K for chemical processes). Temperatures near absolute zero
  or above 5000 K indicate specification errors.
- **Pressure**: should be positive and within expected bounds. Negative pressure
  is non-physical.
- **Conservation of mass**: total mass in should equal total mass out (within
  < 0.1%). Large violations indicate disconnected streams or solver issues.
- **Negative duties**: a heater should have positive duty; a cooler should have
  negative duty. If reversed, the unit is doing the opposite of what was intended.
- **Vapor fraction**: should be between 0 and 1. A value exactly at 0 or 1 when
  you expect two phases may indicate that T/P conditions are outside the
  two-phase region.

## When to Rebuild

If multiple units fail simultaneously and error messages are unclear:
1. Strip the flowsheet back to just the feed streams.
2. Add units one at a time, solving after each.
3. When the failing unit is identified, vary its specifications to find a
   feasible operating point.
4. If the unit still fails, try a different property package or simplify the
   chemistry.

See [references/common-errors.md](references/common-errors.md) for a table of
DWSIM-specific error messages and their remedies.
