# DWSIM Property Codes (PROP\_\*)

DWSIM uses property codes in the format `PROP_XX_N` to identify specific
properties for sensitivity analysis and optimization. Below are the codes
used by the DWSIM tools in this repository (verified against the tool
implementations in `dwsim_tools/tools/`).

## Material Streams (PROP\_MS\_\*)

| Code | Property | Units |
|------|----------|-------|
| `PROP_MS_0` | Temperature | K |
| `PROP_MS_1` | Pressure | Pa |
| `PROP_MS_2` | Total mass flow | kg/s |
| `PROP_MS_3` | Total molar flow | mol/s |
| `PROP_MS_4` | Total volumetric flow | m³/s |

## Heaters (PROP\_HT\_\*)

| Code | Property | Units |
|------|----------|-------|
| `PROP_HT_0` | Pressure drop | Pa |
| `PROP_HT_1` | Efficiency | % |
| `PROP_HT_2` | Outlet temperature | K |
| `PROP_HT_3` | Heat duty | kW |

## Coolers (PROP\_CL\_\*)

Coolers use a separate prefix from heaters.

| Code | Property | Units |
|------|----------|-------|
| `PROP_CL_0` | Pressure drop | Pa |
| `PROP_CL_1` | Efficiency | % |
| `PROP_CL_2` | Outlet temperature | K |
| `PROP_CL_3` | Heat duty | kW |

## Pumps (PROP\_PU\_\*)

| Code | Property | Units |
|------|----------|-------|
| `PROP_PU_1` | Outlet pressure | Pa |
| `PROP_PU_2` | Efficiency | % |
| `PROP_PU_3` | Power (duty) | W |

## Compressors (PROP\_CO\_\*)

| Code | Property | Units |
|------|----------|-------|
| `PROP_CO_1` | Outlet pressure | Pa |
| `PROP_CO_2` | Power (duty) | W |
| `PROP_CO_3` | Adiabatic efficiency | % |

## Valves (PROP\_VA\_\*)

| Code | Property | Units |
|------|----------|-------|
| `PROP_VA_1` | Outlet pressure | Pa |

## Separators / Flash Drums (PROP\_SV\_\*)

| Code | Property | Units |
|------|----------|-------|
| `PROP_SV_0` | Override temperature | K |
| `PROP_SV_1` | Override pressure | Pa |

## Distillation Columns (PROP\_DC\_\*)

| Code | Property | Units |
|------|----------|-------|
| `PROP_DC_2` | Reflux ratio | — |
| `PROP_DC_7` | Reboiler duty | W |

## Usage Notes

- Property codes are case-sensitive: use exactly `PROP_MS_0`, not `Prop_MS_0`.
- Heaters and coolers use **different** prefixes (`PROP_HT_*` vs `PROP_CL_*`).
- Valves use `PROP_VA_*` (not `PROP_VL_*`); separators use `PROP_SV_*` (not
  `PROP_SP_*`).
- These codes are passed as strings to `variable_property` and
  `objective_property` in `run_sensitivity_analysis` and `run_optimization`.
- To discover all available codes on any object at runtime, use the
  `list_object_properties` tool.
- Not all properties are writable; the sensitivity/optimization tools handle
  the read/write distinction internally.
