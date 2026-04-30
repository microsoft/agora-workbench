---
name: property-prediction
description: Predict glass transition temperature (Tg) for acid/epoxide SMILES pairs using the VAE encoder and property prediction head.
parent_skill: vitrimer-inverse-design
---

# Property Prediction

Use this skill when the user has specific acid and epoxide SMILES and wants a fast ML prediction of the glass transition temperature.

## Calling the Tool

```python
result = predict_tg(
    acid_smiles=["OC(=O)CCCCC(=O)O", "OC(=O)c1ccccc1C(=O)O"],
    epoxide_smiles=[
        "C(C1CO1)Oc1ccc(C(C)(C)c2ccc(OCC3CO3)cc2)cc1",
        "C(C1CO1)Oc1ccc(C(C)(C)c2ccc(OCC3CO3)cc2)cc1",
    ],
)
```

## How It Works

1. Tensorize each acid–epoxide pair into tree/graph representations
2. Encode through the hierarchical GNN encoder → 128-dim latent vector `z`
3. Pass `z` through the property prediction head (2-layer MLP: 128 → 64 → 1)
4. Inverse-transform the normalized prediction using the training scaler (mean=373 K, std=32.9 K)

The prediction is fast (~seconds for batches of 32) and does not require MD simulation.

## Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `acid_smiles` | list[str] | List of acid SMILES strings |
| `epoxide_smiles` | list[str] | List of epoxide SMILES strings (same length as acids) |

## Return Values

| Field | Type | Description |
|-------|------|-------------|
| `tg_predicted` | list[float] | Predicted Tg values in Kelvin |
| `latent_vectors` | list[list[float]] | 128-dim latent vectors for each pair |

## Using the Latent Vectors

The returned `latent_vectors` are useful for:

- **Similarity analysis**: Compute Euclidean distance between latent vectors to assess chemical similarity
- **Visualization**: Project with PCA (available via `search_neighbors` or `interpolate_molecules` which return `pca_coords`)
- **Downstream tools**: The latent representation captures both structural and property information

## Important Notes

### Tg scale
Predictions are in Kelvin on the **ML/MD scale**, not the experimental scale. For experimental estimates, use `calibrate_tg` after obtaining MD-validated Tg values from `vitrimer_tg_sim`.

### NaN predictions
If a SMILES pair fails to tensorize (invalid structure or unsupported atom types), the corresponding Tg will be `NaN` and the latent vector will be filled with `NaN`. Check for this when processing results.

### Batch processing
Pairs are processed in batches of 32. For large lists, all pairs are handled automatically — no need to manually chunk.

### SMILES format
- Acid SMILES should represent carboxylic acid monomers
- Epoxide SMILES should represent epoxide (oxirane) monomers
- SMILES are canonicalized internally; different representations of the same molecule will give the same result
