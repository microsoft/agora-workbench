# Impedance Calculation

## Overview

Every transmission line in a PyPSA network needs impedance values:
- **R** (resistance, Ohms) — real power losses
- **X** (reactance, Ohms) — reactive power and voltage regulation
- **B** (susceptance, Siemens) — line charging capacitance

## Formula

```
R = per_km_R × length_km × bundle_factor
X = per_km_X × length_km × bundle_factor
B = per_km_B × length_km / bundle_factor   (capacitance INCREASES with bundling)
```

## Line Length Priority

1. **RAW sheet lengths** (best) — actual model lengths from PSLF simulation, in miles. Convert: `km = miles × 1.609344`
2. **Haversine × 1.3** (fallback) — great-circle distance between bus coordinates with 1.3 detour factor
3. **Absolute minimum** — use 1.0 km if no coordinates available

### Haversine Distance

```python
import math
def haversine_km(lon1, lat1, lon2, lat2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
```

## Voltage Class Snapping

Snap actual kV values to the nearest standard class:

```python
def snap_kv(kv):
    classes = [69, 115, 138, 161, 230, 345, 500, 765]
    return min(classes, key=lambda c: abs(c - kv))
```

Example: `snap_kv(232.5)` → `230`

## Per-km Impedance Table

Standard values for a single conductor per phase:

| kV | R (Ω/km) | X (Ω/km) | B (S/km) |
|----|-----------|-----------|----------|
| 69 | 0.1150 | 0.4100 | 3.400e-6 |
| 115 | 0.0590 | 0.3400 | 4.000e-6 |
| 138 | 0.0450 | 0.3200 | 4.200e-6 |
| 161 | 0.0380 | 0.3000 | 4.500e-6 |
| 230 | 0.0280 | 0.3150 | 4.750e-6 |
| 345 | 0.0150 | 0.2900 | 5.200e-6 |
| 500 | 0.0090 | 0.2800 | 5.800e-6 |
| 765 | 0.0055 | 0.2600 | 6.400e-6 |

## SIL Bundled Conductor Adjustment

**Surge Impedance Loading (SIL)** is the natural loading of a single conductor:

| kV | SIL (MW) |
|----|----------|
| 69 | 13 |
| 115 | 35 |
| 138 | 50 |
| 161 | 68 |
| 230 | 140 |
| 345 | 400 |
| 500 | 900 |
| 765 | 2200 |

If a line's MVA rating exceeds `SIL × 1.5`, it uses bundled conductors and impedance drops:

```python
sil = SIL_MW[snapped_kv]
bundle_factor = 1.0
if rating_mva > sil * 1.5:
    n_conductors = rating_mva / sil
    bundle_factor = 1.0 / n_conductors
    # R and X decrease by bundle_factor
    # B increases (divide by bundle_factor = multiply by n_conductors)
```

**Example:** 230 kV line rated 1400 MVA → SIL=140, n_conductors=10 → impedance is 1/10th of single conductor.

## Adding Lines to PyPSA

```python
n.add("Line", line_name,
    bus0=f"bus_{fr}",
    bus1=f"bus_{to}",
    r=r_ohm,       # computed R
    x=x_ohm,       # computed X
    b=b_siemens,    # computed B
    s_nom=rating_mva,
    length=length_km,
)
```

## Rating Fallback

If a line has no rating from the Summary or RAW sheets, use 750 MVA as default.

## Limitations

- Per-km values are generic — real values depend on conductor type (Drake, Bluebird ACSR, etc.)
- Bundling uses continuous ratio, not discrete bundle counts (2, 3, 4)
- No temperature derating modeled
