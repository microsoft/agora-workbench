---
name: molecule-generation
description: Generate novel vitrimer molecules by sampling from the VAE latent space, or assess reconstruction fidelity by round-tripping molecules through the encoder-decoder.
parent_skill: vitrimer-inverse-design
---

# Molecule Generation

Use this skill when the user wants to generate new vitrimer candidates or test the VAE's ability to faithfully reconstruct known molecules.

## Sampling Novel Molecules

The `sample_molecules` tool draws random points from the standard normal distribution in latent space and decodes them into acid–epoxide SMILES pairs.

```python
result = sample_molecules(
    num_samples=20,   # number of valid molecules to return
    seed=1,           # reproducibility
)
```

### How it works

1. Sample `z ~ N(0, I)` in 128-dimensional latent space
2. Decode z → acid SMILES + epoxide SMILES
3. Validate with RDKit (reject invalid molecules)
4. Predict Tg using the property head
5. Repeat until `num_samples` valid pairs are collected

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `num_samples` | int | 20 | Target number of valid molecules |
| `seed` | int | 1 | Random seed for reproducibility |

### Return Values

| Field | Type | Description |
|-------|------|-------------|
| `acids` | list[str] | Acid SMILES strings |
| `epoxides` | list[str] | Epoxide SMILES strings |
| `tg_predicted` | list[float] | Predicted Tg in Kelvin |
| `num_valid` | int | Count of valid molecules returned |
| `num_attempted` | int | Total decoding attempts (indicates hit rate) |

### Tips

- The VAE's validity rate is typically 30–60%, so `num_attempted` will be 2–3× `num_valid`
- Use larger `num_samples` (50–100) for diverse exploration
- Different `seed` values produce different molecular populations
- Molecules are canonicalized — duplicates across runs are possible for common structures

### Runtime and Timeout

- `num_samples=20`: ~30–90 seconds
- `num_samples=50–100`: ~1–3 minutes
- No need to set explicit timeouts — the server default is generous

## Reconstruction Quality Check

The `reconstruct_molecules` tool encodes molecules through the VAE and decodes them back, measuring how faithfully the round-trip preserves the structure.

```python
result = reconstruct_molecules(
    acid_smiles=["CC(=O)O", "OC(=O)CCCCC(=O)O"],
    epoxide_smiles=["C1OC1c1ccccc1", "C(C1CO1)Oc1ccccc1"],
)
```

### Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `acid_smiles` | list[str] | Acid SMILES to reconstruct |
| `epoxide_smiles` | list[str] | Epoxide SMILES to reconstruct |

### Return Values

| Field | Type | Description |
|-------|------|-------------|
| `acid_original` | list[str] | Input acid SMILES |
| `epoxide_original` | list[str] | Input epoxide SMILES |
| `acid_reconstructed` | list[str] | Reconstructed acid SMILES |
| `epoxide_reconstructed` | list[str] | Reconstructed epoxide SMILES |
| `acid_match` | list[bool] | Per-molecule exact match flags |
| `epoxide_match` | list[bool] | Per-molecule exact match flags |
| `reconstruction_accuracy` | float | Fraction of pairs where both acid and epoxide match (0.0–1.0) |

### Interpreting Accuracy

- **>90%**: Excellent — the VAE has learned these chemical motifs well
- **70–90%**: Good — minor structural changes, predictions still reliable
- **<70%**: The molecules may be outside the training distribution; treat Tg predictions with caution

## When to Use Which

| Goal | Tool |
|------|------|
| Explore diverse new chemistries | `sample_molecules` |
| Check if known molecules are well-represented by the model | `reconstruct_molecules` |
| Find molecules near a known good candidate | Use `search_neighbors` (latent-space-exploration skill) |
| Optimize toward a target Tg | Use `bayesian_optimize` (optimization skill) |
