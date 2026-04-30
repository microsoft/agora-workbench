---
name: tg-analysis
description: Compute glass transition temperature from density–temperature profiles using bilinear piecewise-linear regression.
parent_skill: vitrimer-tg-estimation
---

# Tg Analysis

Use this skill after `run_tg_production` to compute the glass transition temperature from the density–temperature data collected during cooling.

## Method

The density–temperature relationship of a polymer exhibits two distinct linear regimes:

```
density
  ▲
  │    rubbery           glassy
  │    regime            regime
  │       ╲                ╱
  │        ╲    ╱─────────╱
  │         ╲  ╱
  │          ╲╱  ← Tg (breakpoint)
  │          ╱╲
  │         ╱  ╲
  │────────╱    ╲
  └──────────────────────────▶ temperature
       100 K    Tg      800 K
```

The `compute_tg` tool fits a **bilinear (2-segment piecewise-linear) model** to each replica's density(T) profile using the `pwlf` library. The breakpoint between the two segments is defined as the MD-simulated glass transition temperature (Tg_MD).

## Calling the Tool

```python
result = compute_tg(work_dir="/path/from/build_vitrimer_box")
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `work_dir` | str | *required* | Top-level directory containing `prod/replica_N/` from `run_tg_production` |

### Return Values

| Field | Type | Description |
|-------|------|-------------|
| `success` | bool | Whether Tg was computed |
| `tg_mean` | float | Mean Tg across replicas (K) |
| `tg_std` | float | Standard deviation of Tg (K) |
| `tg_cv` | float | Coefficient of variation (σ/μ) — lower is better |
| `tg_per_replica` | list | Individual Tg from each replica (K) |
| `num_replicas` | int | Number of replicas with valid Tg fits |
| `density_temperature_summary` | list | Per-replica metadata (Tg, point count, temp range) |
| `error` | str | Error message if computation failed |

## Interpreting Results

### Tg value
- Reported in **Kelvin** (MD scale, not experimental)
- Typical vitrimer Tg_MD values range from ~200 K to ~600 K
- To convert to experimental Tg, use `calibrate_tg` from the `vitrimer_vae` domain

### Coefficient of variation (CV)
- **CV < 0.05**: Excellent consistency across replicas
- **CV 0.05–0.10**: Acceptable; normal stochastic variation
- **CV > 0.10**: Poor consistency; consider running more replicas or checking for simulation artifacts

### Number of replicas
- **5 replicas**: Standard protocol, best statistics
- **3–4 replicas**: Still reliable if 1–2 failed
- **1–2 replicas**: Use with caution; report the limited statistics

## Example Output

```python
{
    "success": True,
    "tg_mean": 412.35,
    "tg_std": 8.72,
    "tg_cv": 0.0212,
    "tg_per_replica": [405.1, 410.8, 415.2, 419.3, 411.4],
    "num_replicas": 5,
    "density_temperature_summary": [
        {"replica": "replica_1", "tg": 405.1, "num_points": 70, "temp_range": [100.2, 799.8]},
        ...
    ],
    "error": None,
}
```

## Connecting to Experimental Tg

MD-simulated Tg values are systematically offset from experimental values due to the extremely fast cooling rate (10 K per 50 ps ≈ 2×10¹¹ K/s vs. ~10 K/min experimentally). To map onto the experimental scale, use the `calibrate_tg` tool from the `vitrimer_vae` domain:

```python
# After compute_tg
calibrated = calibrate_tg(
    acid_smiles=[acid],
    epoxide_smiles=[epoxide],
    tg_md=[tg_result["tg_mean"]],
)
```

This applies a Gaussian Process with Tanimoto kernel trained on paired MD/experimental data to produce a calibrated Tg estimate.
