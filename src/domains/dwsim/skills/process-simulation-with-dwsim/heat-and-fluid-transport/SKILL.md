---
name: heat-and-fluid-transport
parent_skill: process-simulation-with-dwsim
description: Heat, cool, compress, pump, expand, and exchange heat between process streams using DWSIM unit operations.
states: [dwsim.flowsheet_exists, dwsim.flowsheet_solved]
---

# Heat and Fluid Transport

Use this skill when the user needs to change a stream's temperature or pressure,
exchange heat between two streams, or combine/split process streams.

## Heaters and Coolers

### Heater

Call `add_heater` to raise a stream's temperature:
- `flowsheet`: the flowsheet object.
- `name`: e.g. `"HTR-1"`.
- `inlet_stream_name`: the stream to heat.
- `outlet_stream_name`: tag for the heated outlet stream.
- `outlet_temperature`: target temperature in Kelvin.
- `pressure_drop`: pressure loss across the heater in Pascal (use 0 for no drop).
- `energy_stream_name` (optional): energy stream tag to read the heat duty.

### Cooler

Call `add_cooler` to lower a stream's temperature:
- `flowsheet`: the flowsheet object.
- `name`: e.g. `"CLR-1"`.
- `inlet_stream_name`: the stream to cool.
- `outlet_stream_name`: tag for the cooled outlet stream.
- `outlet_temperature`: target temperature in Kelvin.
- `pressure_drop`: pressure loss across the cooler in Pascal.
- `energy_stream_name` (optional): energy stream tag to read the rejected heat.

Use a heater when the outlet temperature is above the inlet, and a cooler when
the outlet is below. Both calculate the duty (energy required) automatically.

## Heat Exchangers

Call `add_heat_exchanger` to transfer heat between a hot stream and a cold stream:
- `flowsheet`: the flowsheet object.
- `name`: e.g. `"HX-1"`.
- `hot_inlet`: tag of the hot-side inlet stream.
- `hot_outlet`: tag for the hot-side outlet stream.
- `cold_inlet`: tag of the cold-side inlet stream.
- `cold_outlet`: tag for the cold-side outlet stream.
- `hot_outlet_temperature`: target temperature for the hot-side outlet in Kelvin.

The cold-side outlet temperature is calculated from the energy balance. The
log-mean temperature difference (LMTD) governs heat transfer — ensure the hot
outlet is warmer than the cold inlet (no temperature cross) for a feasible design.

## Pumps

Call `add_pump` to raise the pressure of a **liquid** stream:
- `flowsheet`: the flowsheet object.
- `name`: e.g. `"PUMP-1"`.
- `inlet_stream_name`: liquid stream to pressurize.
- `outlet_stream_name`: tag for the pressurized outlet.
- `outlet_pressure`: target discharge pressure in Pascal.
- `energy_stream_name` (optional): energy stream for pump work.

Pump efficiency affects the work duty — DWSIM uses a default efficiency. The
stream must be liquid at the inlet conditions; pumping a two-phase stream may
cause errors.

## Compressors

Call `add_compressor` to raise the pressure of a **gas** stream:
- `flowsheet`: the flowsheet object.
- `name`: e.g. `"COMP-1"`.
- `inlet_stream_name`: vapor stream to compress.
- `outlet_stream_name`: tag for the compressed outlet.
- `outlet_pressure`: target discharge pressure in Pascal.
- `energy_stream_name` (optional): energy stream for compressor work.

Higher compression ratios require more work and produce hotter outlets. For
compression ratios above ~4:1, consider multi-stage compression with inter-stage
cooling.

## Valves

Call `add_valve` for isenthalpic pressure letdown:
- `flowsheet`: the flowsheet object.
- `name`: e.g. `"VLV-1"`.
- `inlet_stream_name`: high-pressure stream.
- `outlet_stream_name`: tag for the reduced-pressure outlet.
- `outlet_pressure`: target outlet pressure in Pascal.

Expansion across a valve is isenthalpic (constant enthalpy). For gases, this
causes cooling (Joule-Thomson effect). For liquids, if the outlet pressure drops
below the bubble point, partial vaporization (flashing) occurs.

## Expanders / Turbines

Call `add_expander` to extract work from a gas or steam stream by reducing its
pressure. Used in power cycles (Brayton, Kalina), refrigeration cascades, and
process letdown where work recovery is desired.

Parameters:
- `flowsheet`: the flowsheet object.
- `name`: e.g. `"EXP-1"`.
- `inlet_stream_name`: high-pressure gas or steam stream.
- `outlet_stream_name`: tag for the expanded outlet stream.
- `outlet_pressure`: target discharge pressure in Pascal.
- `efficiency` (optional): adiabatic efficiency in percent (default 75%).
- `energy_stream_name` (optional): energy stream tag receiving the generated work.

### Expander vs. Valve

| Feature | Expander | Valve |
|---------|----------|-------|
| Work recovery | Yes (generates power) | No (isenthalpic) |
| Outlet temperature | Lower (isentropic expansion) | Depends on Joule-Thomson |
| Typical use | Power cycles, large pressure drops | Small pressure letdowns |
| Cost | Expensive (rotating equipment) | Cheap |

Use an expander when the pressure drop is large and work recovery is
economically justified. Use a valve for small pressure adjustments or when
simplicity is preferred.

## Mixers

Call `add_mixer` to combine multiple inlet streams:
- `flowsheet`: the flowsheet object.
- `name`: e.g. `"MIX-1"`.
- `inlet_stream_names`: comma-separated tags, e.g. `"STREAM-1,STREAM-2"`.
- `outlet_stream_name`: tag for the mixed outlet.

The mixer performs an adiabatic mixing calculation — outlet temperature is
determined by energy balance.

## Splitters

Call `add_splitter` to divide a stream by molar split ratios:
- `flowsheet`: the flowsheet object.
- `name`: e.g. `"SPL-1"`.
- `inlet_stream_name`: stream to split.
- `outlet_stream_names`: comma-separated outlet tags, e.g. `"PROD,RECYCLE"`.
- `split_ratios`: comma-separated ratios that sum to 1, e.g. `"0.7,0.3"`.

Each outlet has the same composition as the inlet but a fraction of the total flow.

See [references/equipment-guidelines.md](references/equipment-guidelines.md) for
typical operating ranges and design rules.
