# Tg Determination Protocol Reference

## Publication Protocol

Based on the MD simulation protocol for vitrimer glass transition temperature
estimation from the vitrimer dataset publication.

## Equilibration Sequence

```
Initial box (0.5 g/cm³)
    │
    ▼  Conjugate gradient minimization
    │
    ▼  NVT 300 K, 50 ps (dt=0.5 fs)
    │
    ▼  NPT 300 K, 1 atm, 100 ps
    │  → density adjusts to realistic level
    │
    ▼  NPT 300→800 K, 1 atm, 500 ps
    │  → annealing removes local heterogeneities
    │
    ▼  NPT 800 K, 1 atm, 50 ps
    │  → write 5 restart snapshots at 10 ps intervals
    │
    5 independent starting configurations
```

## Production Cooling

```
Per replica (×5, in parallel):

800 K ──[25 ps ramp]──▶ 790 K ──[25 ps hold]──▶ record density
790 K ──[25 ps ramp]──▶ 780 K ──[25 ps hold]──▶ record density
  ...
110 K ──[25 ps ramp]──▶ 100 K ──[25 ps hold]──▶ record density

Total: 70 cooling steps → 71 temperature points
       70 × 50 ps = 3500 ps per replica
       50000 steps per ramp, 50000 steps per hold (dt=0.5 fs)
```

## Density Averaging

During each 25 ps hold phase, density is averaged using LAMMPS `fix ave/time`:

```
fix avg all ave/time 100 500 50000 ... file step_N.txt
```

- Sample every 100 steps
- Window of 500 samples
- Output every 50000 steps (= 1 average per hold phase)

This yields one averaged density value per temperature point.

## Bilinear Fitting

The density–temperature profile is fit with a 2-segment piecewise-linear model:

```python
import pwlf
model = pwlf.PiecewiseLinFit(temperature, density)
breakpoints = model.fit(2)  # [T_min, Tg, T_max]
tg = breakpoints[1]
```

The breakpoint between the glassy (steep slope) and rubbery (shallow slope)
regimes is defined as Tg_MD.

## Statistical Averaging

- 5 replicas per compound
- Mean Tg = average of 5 breakpoints
- CV = std / mean (coefficient of variation)
- Replicas with Tg ≤ 0 are excluded as non-physical

## Cooling Rate Effects

The MD cooling rate (~2×10¹¹ K/s) is many orders of magnitude faster than
experimental DSC (~10 K/min). This causes a systematic overestimation of Tg.
The `calibrate_tg` tool in the `vitrimer_vae` domain corrects for this
using a Gaussian Process trained on paired MD/experimental Tg data.
