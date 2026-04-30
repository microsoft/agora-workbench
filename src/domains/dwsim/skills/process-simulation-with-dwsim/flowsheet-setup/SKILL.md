---
name: flowsheet-setup
parent_skill: process-simulation-with-dwsim
description: Create and initialize DWSIM flowsheets, select compounds and property packages, add inlet streams, save flowsheets, and configure recycle loops.
states: [dwsim.compounds_available, dwsim.flowsheet_exists]
---

# Flowsheet Setup

Use this skill when the user wants to start a new simulation, create a flowsheet
from scratch, or doesn't yet have a flowsheet to work with. Also use it when
loading an existing `.dwxmz` or `.dwxml` file.

## Compound Selection

Before creating a flowsheet, verify that the compound names you plan to use exist
in the DWSIM database. Call `search_compounds` with a substring query to find
exact names:

- `search_compounds` with `query: "ethanol"` → returns `["Ethanol", ...]`
- `search_compounds` with `query: "acet"` → returns `["Acetone", "Acetic acid", ...]`
- `search_compounds` with no query → returns the full compound list.

Always use the exact names returned by `search_compounds` when calling
`create_flowsheet`. Misspelled or invented names will cause creation to fail.

## Property Package Selection

Choose the thermodynamic property package based on the chemistry involved.
A decision tree:

| Chemistry | Recommended Package | Notes |
|-----------|-------------------|-------|
| Ideal / low-pressure gas mixtures | Peng-Robinson or SRK | Good general-purpose cubic EOS |
| Polar / non-ideal liquid mixtures (alcohols, water, amines) | NRTL or UNIQUAC | Handles liquid-phase non-ideality |
| Strongly non-ideal without binary data | UNIFAC or Modified UNIFAC (Dortmund) | Predictive; no fitted parameters needed |
| Pure steam / water systems | Steam Tables (IAPWS-IF97) | High-accuracy water properties |
| Refrigerants or specialty fluids | CoolProp | Covers many industrial fluids |
| Simple ideal-liquid systems | Raoult's Law | Simplest model; limited applicability |
| Light hydrocarbons at moderate pressure | Lee-Kesler-Plocker | Correlation-based for hydrocarbons |

When uncertain, prefer **NRTL** for liquid-phase non-ideality or **Peng-Robinson**
for gas-phase systems. See [references/property-packages.md](references/property-packages.md)
for the full guide.

## Creating a Flowsheet

Call `create_flowsheet` with:
- `compounds`: comma-separated compound names, e.g. `"Water,Ethanol,Methanol"`
- `property_package`: one of the supported package names listed above.

The tool returns a dict containing the **flowsheet object** — store the flowsheet
in a variable and pass it to subsequent tool calls. Without it, no unit operations or streams can be added.

Example workflow:
1. `search_compounds` with `query: "water"` → confirm `"Water"` exists.
2. `search_compounds` with `query: "ethanol"` → confirm `"Ethanol"` exists.
3. `create_flowsheet` with `compounds: "Water,Ethanol"`, `property_package: "NRTL"`.

## Adding Inlet Material Streams

Call `add_material_stream` for each feed entering the process:
- `flowsheet`: the flowsheet object from `create_flowsheet`.
- `name`: a short, uppercase, descriptive tag (e.g. `"FEED"`, `"SOLVENT"`).
- `temperature`: in Kelvin (e.g. 298.15 K for 25 °C).
- `pressure`: in Pascal (e.g. 101325 Pa for 1 atm).
- `compound_mole_fractions`: a JSON string mapping compound names to mole fractions.
  **Fractions must sum to 1.0**.
  Example: `'{"Water": 0.6, "Ethanol": 0.4}'`
- `total_molar_flow`: in mol/s.

## Adding Energy Streams

Call `add_energy_stream` when you need to track heat or work duty for a unit
operation (heaters, coolers, pumps, compressors, reactors). Parameters:
- `flowsheet`: the flowsheet object.
- `name`: e.g. `"Q-HTR-1"` or `"W-PUMP-1"`.

Energy streams are optional — if you omit the `energy_stream_name` parameter on
unit operations that accept it, the tool will skip the connection. Create energy
streams only when you want to read the duty later.

## Naming Conventions

Use short, uppercase, descriptive tags for all streams and units:
- Streams: `FEED`, `PRODUCT`, `RECYCLE`, `VAPOR-OUT`, `LIQ-OUT`
- Energy streams: `Q-HTR-1`, `W-PUMP-1`
- Unit operations: `HTR-1`, `FLASH-1`, `COLUMN-1`, `RX-1`

## Solving the Flowsheet

Once all streams and unit operations are added, call `solve_flowsheet`:
- `flowsheet`: the flowsheet object.

Always check the response before reading results:
- `converged: true` — the flowsheet solved without errors; proceed to extract results.
- `converged: false` — one or more objects failed. Read `error_messages` to identify the problem, fix the specification, and re-solve.

Example:
```
solve_flowsheet(flowsheet=fs)
→ {"converged": true, "error_messages": []}
```

If errors appear, call `get_flowsheet_summary` for a per-object breakdown, then correct the failing unit and re-solve.

## Loading Existing Flowsheets
- `file_path`: absolute path to a `.dwxmz` or `.dwxml` file on the server.

This returns a flowsheet object just like `create_flowsheet`, allowing you to
modify, solve, and extract results from pre-built models.

## Saving Flowsheets

Call `save_flowsheet` to persist a flowsheet to disk:
- `flowsheet`: the flowsheet object.
- `file_path`: absolute path on the server, e.g. `"/tmp/my_process.dwxmz"`.

Use the `.dwxmz` extension for compressed format or `.dwxml` for plain XML.
Parent directories are created automatically if they do not exist.

## Recycle Loops

Call `add_recycle` when the process has a recycle loop (e.g. unreacted feed
returned to the reactor inlet). The recycle block iterates until the assumed
and calculated stream values converge.

Parameters:
- `flowsheet`: the flowsheet object.
- `name`: e.g. `"RCY-1"`.
- `inlet_stream_name`: the stream coming **from** downstream (the "calculated" values).
- `outlet_stream_name`: the stream going **to** upstream (the "assumed" values).
- `max_iterations` (optional): convergence limit (default 100).
- `tolerance_mass_flow` (optional): relative tolerance for mass flow (default 1e-3).
- `tolerance_temperature` (optional): relative tolerance for temperature (default 1e-3).
- `tolerance_pressure` (optional): relative tolerance for pressure (default 1e-3).
- `acceleration_method` (optional): `"Wegstein"` (default, recommended) or `"Direct"`.

### Recycle Best Practices

1. **Build without the recycle first**: get the open-loop flowsheet to converge, then
   add the recycle block and re-solve.
2. **Initialize the assumed stream**: give the recycle outlet stream a reasonable
   initial guess (composition, T, P, flow) matching what you expect at steady state.
3. **Use Wegstein acceleration**: it converges significantly faster than direct
   substitution for most chemical processes.
4. **Check convergence**: after solving, verify that the recycle inlet and outlet
   streams have matching values (call `get_stream_results` on both).
