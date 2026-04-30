---
name: latent-space-exploration
description: Explore the vitrimer chemical space by finding molecular neighbors around a query compound or interpolating between two molecules in the VAE latent space.
parent_skill: vitrimer-inverse-design
---

# Latent Space Exploration

Use this skill when the user wants to find molecules similar to a known vitrimer, or generate a smooth transition path between two vitrimers.

## Finding Neighbors

The `search_neighbors` tool perturbs a query molecule's latent vector with controlled noise to discover similar but distinct vitrimers.

```python
result = search_neighbors(
    acid_smiles="OC(=O)CCCCC(=O)O",
    epoxide_smiles="C(C1CO1)Oc1ccc(C(C)(C)c2ccc(OCC3CO3)cc2)cc1",
    search_type="both",     # "acid", "epoxide", or "both"
    num_neighbors=100,
    max_noise=20.0,
    seed=1,
)
```

### Search Types

The VAE latent space has structured subspaces, enabling targeted exploration:

| `search_type` | What varies | What stays fixed | Use case |
|---------------|-------------|------------------|----------|
| `"acid"` | Acid-specific dims (8d) | Epoxide + shared dims | Explore acid substitutions for a fixed epoxide |
| `"epoxide"` | Epoxide-specific dims (8d) | Acid + shared dims | Explore epoxide substitutions for a fixed acid |
| `"both"` | Full latent space (128d) | Nothing fixed | Broad neighborhood exploration |

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `acid_smiles` | str | *required* | Query acid SMILES |
| `epoxide_smiles` | str | *required* | Query epoxide SMILES |
| `search_type` | str | `"both"` | Subspace to perturb |
| `num_neighbors` | int | 100 | Number of noise samples to generate |
| `max_noise` | float | 20.0 | Maximum noise magnitude |
| `seed` | int | 1 | Random seed |

### Return Values

| Field | Type | Description |
|-------|------|-------------|
| `acids` | list[str] | Neighbor acid SMILES (sorted by distance) |
| `epoxides` | list[str] | Neighbor epoxide SMILES |
| `tg_predicted` | list[float] | Predicted Tg for each neighbor (K) |
| `distances` | list[float] | Latent-space Euclidean distance from query |
| `pca_coords` | list[list[float]] | 2D PCA coordinates for visualization |

### Tips

- **Small `max_noise` (1–5)**: Minor variations, highly similar molecules
- **Medium `max_noise` (5–20)**: Moderate diversity, still chemically related
- **Large `max_noise` (>20)**: Wide exploration, may find very different structures
- Increase `num_neighbors` for better coverage; duplicates are filtered out
- Results are sorted by distance — closest neighbors appear first

### Runtime and Timeout

- A single `search_neighbors` call with 100 neighbors takes ~30–120 seconds
- `interpolate_molecules` with 20 points takes ~10–60 seconds
- No need to set explicit timeouts — the server default is generous

## Interpolating Between Molecules

The `interpolate_molecules` tool generates a smooth path between two vitrimer endpoints in latent space.

```python
result = interpolate_molecules(
    acid1="OC(=O)CCCCC(=O)O",            # Start acid
    epoxide1="C(C1CO1)Oc1ccccc1",         # Start epoxide
    acid2="OC(=O)c1ccccc1C(=O)O",         # End acid
    epoxide2="C(C1CO1)c1ccccc1",           # End epoxide
    method="linear",                       # or "spherical"
    num_points=20,
    seed=5,
)
```

### Interpolation Methods

| Method | How it works | When to use |
|--------|-------------|-------------|
| `"linear"` | `z = α·z_end + (1-α)·z_start` | Default; good for nearby molecules |
| `"spherical"` | Great-circle (SLERP) interpolation | Better for distant molecules; stays on the latent manifold |

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `acid1`, `epoxide1` | str | *required* | Start-point SMILES |
| `acid2`, `epoxide2` | str | *required* | End-point SMILES |
| `method` | str | `"linear"` | Interpolation method |
| `num_points` | int | 20 | Number of intermediate points |
| `seed` | int | 5 | Random seed |

### Return Values

| Field | Type | Description |
|-------|------|-------------|
| `acids` | list[str] | Acid SMILES along the path (sorted by distance from start) |
| `epoxides` | list[str] | Epoxide SMILES along the path |
| `tg_predicted` | list[float] | Predicted Tg along the path (K) |
| `distances` | list[float] | Distance from the start point |
| `pca_coords` | list[list[float]] | 2D PCA coordinates for visualization |

### Interpreting Results

- The start and end molecules are included in the output
- **Tg gradient**: Watch how `tg_predicted` changes along the path — this reveals the Tg landscape between the endpoints
- **Structural transitions**: Note where acid/epoxide SMILES change discontinuously — these are boundaries between decoded molecular identities
- Duplicate molecules along the path are removed; gaps indicate regions where decoding produces the same structure

## Visualization

Both tools return `pca_coords` (2D projections of the 128-dim latent vectors). Plot these to visualize the exploration:

```python
import matplotlib.pyplot as plt

coords = result["pca_coords"]
tg = result["tg_predicted"]
x = [c[0] for c in coords]
y = [c[1] for c in coords]
plt.scatter(x, y, c=tg, cmap="coolwarm")
plt.colorbar(label="Predicted Tg (K)")
plt.xlabel("PCA 1")
plt.ylabel("PCA 2")
```
