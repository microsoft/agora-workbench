# Common DWSIM Error Messages

## Error Message Reference

| Error Message | Meaning | Remedy |
|--------------|---------|--------|
| "Flash calculation error" | The thermodynamic flash routine failed to find a valid phase split at the specified T, P, and composition | Check that T and P are within the valid range for the property package. Try a different property package if the system is near a phase boundary. |
| "Could not find compound" | A compound name in the flowsheet or reaction does not match the DWSIM database | Use `search_compounds` to verify the exact name. DWSIM compound names are case-sensitive. |
| "Mole fractions do not sum to 1" | The `compound_mole_fractions` JSON values don't add up to exactly 1.0 | Recalculate fractions. Use sufficient decimal places to ensure the sum is 1.0. |
| "Object not connected" | A unit operation has an unconnected inlet or outlet port | Verify that all stream names passed to the unit operation exist and are spelled correctly. |
| "Singular Jacobian" (distillation) | The column solver's matrix became singular, typically due to an infeasible specification | Relax specifications: increase reflux ratio, increase number of stages, or adjust reboiler duty. Start with a simpler column and tighten specifications gradually. |
| "Maximum iterations reached" | The solver hit the iteration limit without converging | The problem may be poorly initialized. Try better initial guesses, or build the flowsheet incrementally. |
| "Enthalpy calculation error" | The property package cannot compute enthalpy at the given conditions | Usually occurs at extreme T/P. Verify conditions are within the property package's valid range. |
| "Pressure specification error" | Outlet pressure is higher than inlet for a valve, or lower than inlet for a pump/compressor | Check that the pressure direction is correct for the equipment type. |
| "Negative flow" | A stream has a computed negative molar or mass flow | Usually caused by mole fractions not summing to 1.0, or by an unbalanced reaction that consumes more than available. |
| "Property package error" | General failure in the thermodynamic model | May indicate missing binary interaction parameters. Try UNIFAC (predictive) if fitted parameters are unavailable. |

## General Troubleshooting Steps

1. **Read the error message carefully** — DWSIM error messages usually point to the specific object and cause.
2. **Check `get_flowsheet_summary`** — the `object_list` shows per-object error status.
3. **Isolate the problem** — if multiple objects fail, strip back to a simpler flowsheet and add units one at a time.
4. **Verify inputs** — check compound names, mole fractions, units (K not °C, Pa not bar), and stream connections.
5. **Try a different property package** — if flash calculations fail consistently, the thermodynamic model may not be suitable for the chemistry.
