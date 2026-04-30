---
name: vitrimer-inverse-design
description: AI-guided inverse design of recyclable vitrimeric polymers using a hierarchical graph neural network VAE for molecule generation, property prediction, latent-space exploration, calibration, and Bayesian optimization.
parent_skill: vitrimer
---

# Vitrimer Inverse Design

Use this skill when the user wants to design new vitrimer polymers with target glass transition temperatures, explore the chemical space of acid–epoxide pairs, or predict Tg for known compositions.

## Sub-Skills

| Sub-skill | When to use |
|-----------|-------------|
| `molecule-generation` | Generate novel vitrimer molecules or reconstruct existing ones through the VAE |
| `property-prediction` | Predict Tg for known acid/epoxide SMILES pairs |
| `latent-space-exploration` | Find similar molecules or interpolate between two vitrimers in the latent space |
| `calibration` | Convert MD-simulated Tg values to experimental scale using a GP model |
| `optimization` | Discover vitrimers targeting a specific Tg via Bayesian optimization |

## Typical Workflows

### Targeted discovery (find vitrimers with a specific Tg)
```
bayesian_optimize(target_tg=450.0)
    │
    ▼  Top candidates (acid + epoxide SMILES, predicted Tg)
    │
    ├──▶ search_neighbors(...)  →  Explore variations around best hits
    │
    └──▶ [vitrimer_tg_sim]     →  Validate with MD simulation
             │
             ▼
         calibrate_tg(tg_md)   →  Experimental-scale Tg
```

### Exploration (understand the chemical space)
```
sample_molecules(num_samples=50)
    │
    ▼  Random diverse molecules with predicted Tg
    │
    ├──▶ interpolate_molecules(mol_A, mol_B)  →  Smooth path between two vitrimers
    │
    └──▶ search_neighbors(query, search_type="acid")  →  Fix epoxide, vary acid
```

### Quality check (validate the VAE model)
```
reconstruct_molecules(acids, epoxides)
    │
    ▼  Reconstruction accuracy + per-molecule match flags
```

## Latent Space Structure

The VAE encodes each acid–epoxide pair into a 128-dimensional latent vector with structured subspaces:

```
z (128 dims) = [ acid-specific (8) │ shared (48) │ epoxide-specific (8) ]
                 ↑                    ↑                ↑
           varies acid only    affects both      varies epoxide only
```

This structure enables targeted exploration: perturb only the acid subspace to vary the acid while keeping the epoxide fixed, or vice versa.

## Model Details

- **Architecture**: Hierarchical Graph Neural Network VAE (HierVAE)
- **Training**: 7,424 acid–epoxide vitrimer pairs
- **Tg predictor**: 2-layer MLP head on the latent vector
- **Tg scale**: Normalized (mean=373 K, std=32.9 K), inverse-transformed for output
- **Checkpoint**: `prop49.model` (epoch 49)

## Code Execution Timeouts

The server timeout is set to 4 hours. Typical wall times at default parameters:

| Tool | Typical runtime |
|------|----------------|
| `predict_tg` | 1–5 s |
| `reconstruct_molecules` | 1–10 s |
| `sample_molecules` (20) | 30–90 s |
| `sample_molecules` (50+) | 1–3 min |
| `search_neighbors` (100) | 30–120 s |
| `interpolate_molecules` | 10–60 s |
| `bayesian_optimize` (default) | 5–30 min |
| `bayesian_optimize` (large pool/iters) | 30 min – 2 h |

Bayesian optimization is the most expensive operation. Runtime scales with
`pool_size × num_iterations`. There is no need to set explicit timeouts —
the defaults are generous enough. Jobs exceeding 4 hours will time out.

## Integration with vitrimer_tg_sim

This domain provides fast ML predictions (~seconds). For physics-based validation:
1. Use tools here to identify promising candidates
2. Send top picks to `vitrimer_tg_sim` for MD simulation (build → equilibrate → produce → compute Tg)
3. Feed MD Tg back into `calibrate_tg` to get experimental-scale estimates
