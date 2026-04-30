---
name: flash-separation
parent_skill: process-simulation-with-dwsim
description: Set up and analyze vapor-liquid flash separations using flash drums and liquid-liquid decanters, including multi-stage flash configurations.
states: [dwsim.flowsheet_exists, dwsim.flowsheet_solved]
---

# Flash Separation

Use this skill when the user asks to separate phases, perform a flash
calculation, set up a flash drum or knock-out drum, or determine dew/bubble
point conditions.

## When to Flash

A flash separator is needed when:
- A mixed-phase feed must be split into vapor and liquid products.
- You want to determine the phase split at specific T and P conditions.
- A gas-liquid knock-out drum is required before compression or downstream processing.
- The user wants to explore bubble or dew point behavior of a mixture.

## Setting Up a Flash Drum

Call `add_separator` with:
- `flowsheet`: the flowsheet object.
- `name`: e.g. `"FLASH-1"`.
- `inlet_stream_name`: tag of the feed stream (must already exist).
- `vapor_outlet_name`: tag for the vapor product stream (will be created).
- `liquid_outlet_name`: tag for the liquid product stream (will be created).
- `temperature`: flash operating temperature in Kelvin. Use `0` for an adiabatic flash.
- `pressure`: flash operating pressure in Pascal.

Example: flash a mixed water-ethanol feed at 80 °C and 1 atm:
1. `add_material_stream` — `name: "FEED"`, `temperature: 353.15`, `pressure: 101325`,
   `compound_mole_fractions: '{"Water": 0.6, "Ethanol": 0.4}'`, `total_molar_flow: 100`.
2. `add_separator` — `name: "FLASH-1"`, `inlet_stream_name: "FEED"`,
   `vapor_outlet_name: "VAP-1"`, `liquid_outlet_name: "LIQ-1"`,
   `temperature: 353.15`, `pressure: 101325`.
3. `solve_flowsheet`.
4. `get_stream_results` on `"VAP-1"` and `"LIQ-1"` to read compositions and flows.

## Interpreting Results

After solving, call `get_stream_results` on both outlet streams:
- **vapor_fraction**: should be 1.0 for the vapor outlet and 0.0 for the liquid outlet.
- **phase_compositions**: mole and mass fractions per compound in each phase.
- **total_molar_flow**: how much of the feed reports to each phase.

The lighter (more volatile) components concentrate in the vapor outlet; heavier
components concentrate in the liquid outlet.

## Multi-Stage Flash

For tighter separation, chain multiple separators with intermediate heaters or
coolers between stages:

1. Flash the feed at an initial T and P → vapor + liquid.
2. Cool or heat the liquid product to a new condition.
3. Flash again at the new T and P → second vapor + second liquid.

Use `add_heater` or `add_cooler` between stages to adjust temperature, and
`add_valve` if pressure reduction is needed.

See [references/vle-fundamentals.md](references/vle-fundamentals.md) for
background on vapor-liquid equilibrium concepts.

## Liquid-Liquid Decanter (Three-Phase Separator)

Call `add_decanter` when you need to separate two immiscible liquid phases — for
example, splitting an organic/aqueous mixture after an azeotropic distillation
overhead condenser, or separating water from a hydrophobic product.

Parameters:
- `flowsheet`: the flowsheet object.
- `name`: e.g. `"DEC-1"`.
- `inlet_stream_name`: tag of the feed stream (should contain two liquid phases).
- `light_liquid_outlet_name`: tag for the light (organic) liquid outlet.
- `heavy_liquid_outlet_name`: tag for the heavy (aqueous) liquid outlet.
- `temperature` (optional): operating temperature in Kelvin (0 for adiabatic).
- `pressure` (optional): operating pressure in Pascal (0 to use feed pressure).
- `energy_stream_name` (optional): energy stream tag.

### How the Decanter Works

The decanter uses DWSIM's three-phase vessel (`TPVessel`) which performs a
three-phase flash: vapor, light liquid, and heavy liquid. A dummy vapor stream
is created automatically — for subcooled feeds the vapor flow will be negligible.

### Decanter Tips

1. **Property package**: NRTL or UNIQUAC are recommended for liquid-liquid
   equilibrium calculations. Peng-Robinson may also work but is less accurate
   for LLE.
2. **Feed condition**: ensure the feed is in the two-liquid-phase region. If the
   feed is above its consolute temperature, only one liquid phase exists and the
   decanter won't split the mixture.
3. **Typical applications**: azeotropic distillation overhead decanters
   (e.g. ethanol/water/benzene), butanol/water separation, solvent recovery
   from aqueous waste streams.
