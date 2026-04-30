---
name: calibration
description: Calibrate MD-simulated glass transition temperatures against experimental data using a Gaussian Process with Tanimoto kernel on Morgan fingerprints.
parent_skill: vitrimer-inverse-design
---

# Tg Calibration

Use this skill when the user has MD-simulated Tg values (from `vitrimer_tg_sim` or other sources) and needs to convert them to the experimental scale.

## Why Calibration Is Needed

MD simulations use extremely fast cooling rates (~2×10¹¹ K/s) compared to experimental DSC measurements (~10 K/min). This systematic difference causes MD-predicted Tg values to be offset from experimental values. The `calibrate_tg` tool corrects this using a structure-aware statistical model.

## How It Works

1. **Vitrimerize**: Combine the acid and epoxide monomers into a full vitrimer polymer SMILES using reaction SMARTS
2. **Fingerprint**: Compute Morgan fingerprints (radius=3, 2048 bits) for the vitrimer
3. **Train GP**: Fit a Gaussian Process on calibration data (tg_experimental − tg_md) using a custom Tanimoto kernel
4. **Predict correction**: Apply the GP to predict the correction for the query molecules
5. **Calibrate**: `Tg_calibrated = Tg_MD + GP_correction`

### The Tanimoto Kernel

The GP uses a custom Tanimoto kernel on binary Morgan fingerprints:

```
K(x, x') = σ² · (x · x') / (||x||² + ||x'||² − x · x')
```

This kernel measures molecular similarity based on shared substructural features, which is more chemically meaningful than Euclidean distance in fingerprint space.

## Calling the Tool

```python
result = calibrate_tg(
    acid_smiles=["OC(=O)CCCCC(=O)O"],
    epoxide_smiles=["C(C1CO1)Oc1ccc(C(C)(C)c2ccc(OCC3CO3)cc2)cc1"],
    tg_md=[425.3],  # MD-simulated Tg from vitrimer_tg_sim
)
```

### Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `acid_smiles` | list[str] | Acid monomer SMILES |
| `epoxide_smiles` | list[str] | Epoxide monomer SMILES |
| `tg_md` | list[float] | MD-simulated Tg values in Kelvin |

All three lists must have the same length.

### Return Values

| Field | Type | Description |
|-------|------|-------------|
| `tg_calibrated` | list[float] | Calibrated Tg estimates on experimental scale (K) |
| `vitrimer_smiles` | list[str] | Generated vitrimer polymer SMILES |

## Calibration Data

The GP is trained on a built-in dataset (`tg_calibration.csv`) containing paired experimental and MD Tg values for ~100+ polymers. The dataset includes:

| Column | Description |
|--------|-------------|
| `smiles` | Polymer SMILES |
| `tg_exp` | Experimental Tg (K) |
| `tg_md` | MD-simulated Tg (K) |
| `std` | Standard deviation of MD Tg across replicas |

## Typical Integration with vitrimer_tg_sim

```python
# 1. Run MD simulation (vitrimer_tg_sim tools)
box = build_vitrimer_box(acid_smiles=acid, epoxide_smiles=epoxide)
run_equilibration(work_dir=box["work_dir"])
run_tg_production(work_dir=box["work_dir"])
tg_result = compute_tg(work_dir=box["work_dir"])

# 2. Calibrate to experimental scale (vitrimer_vae tool)
cal = calibrate_tg(
    acid_smiles=[acid],
    epoxide_smiles=[epoxide],
    tg_md=[tg_result["tg_mean"]],
)
# cal["tg_calibrated"][0] is the experimental-scale estimate
```

## Limitations

- Calibration accuracy depends on how similar the query molecule is to the training set
- Exotic chemistries far from the training distribution may have larger uncertainty
- The vitrimer SMARTS reactions assume standard acid + epoxide ring-opening polymerization; unusual connectivity may fail
- If SMILES parsing fails, the corresponding entry will have an empty vitrimer SMILES
