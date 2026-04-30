# Equipment Design Guidelines

## Typical Pressure Drops

| Equipment | Typical ΔP |
|-----------|-----------|
| Shell-and-tube heat exchanger (tube side) | 10–70 kPa |
| Shell-and-tube heat exchanger (shell side) | 20–50 kPa |
| Air-cooled exchanger | 5–30 kPa |
| Fired heater | 10–50 kPa |
| Packed column (per meter of packing) | 0.1–0.5 kPa |
| Control valve | 50–150 kPa |
| Pipe (per 100 m, typical) | 5–20 kPa |

For preliminary simulation, use 0 Pa pressure drop if the exact value is unknown,
then refine once the base case converges.

## Compression Ratios

| Compressor Type | Typical Max Ratio per Stage |
|----------------|---------------------------|
| Centrifugal | 3:1 to 4:1 |
| Reciprocating | 3:1 to 6:1 |
| Axial | 1.2:1 to 1.5:1 per stage |

For overall ratios exceeding the per-stage limit, use multi-stage compression
with intercooling to near-ambient temperature between stages.

## Heat Exchanger Approach Temperatures

| Service | Minimum Approach ΔT |
|---------|-------------------|
| Liquid-liquid | 10–20 K |
| Gas-gas | 20–50 K |
| Condensing vapor vs liquid | 5–10 K |
| Reboiler | 10–30 K |

The hot outlet should always be warmer than the cold inlet. If the calculated
LMTD is very small (< 5 K), the required heat transfer area becomes impractically
large.

## Pump Efficiency

| Service | Typical Efficiency |
|---------|------------------|
| Centrifugal pump (normal) | 60–85% |
| Centrifugal pump (small / viscous) | 30–60% |
| Positive displacement pump | 70–90% |

DWSIM uses a default adiabatic efficiency. To read the actual duty, connect an
energy stream and check results after solving.

## General Tips

- Always specify temperatures in Kelvin and pressures in Pascal (SI units).
- Pressure drops are cumulative — account for them through the entire process.
- For heat integration, compare duties across heaters and coolers to identify
  opportunities for heat exchange.
