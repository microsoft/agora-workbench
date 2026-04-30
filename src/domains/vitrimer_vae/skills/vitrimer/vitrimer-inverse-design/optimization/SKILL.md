---
name: optimization
description: Discover vitrimer molecules with target glass transition temperature via Bayesian optimization in the VAE latent space.
parent_skill: vitrimer-inverse-design
---

# Bayesian Optimization

Use this skill when the user wants to find vitrimer molecules that achieve a specific Tg target, or maximize Tg outright.

## How It Works

The `bayesian_optimize` tool performs iterative optimization in the 128-dimensional VAE latent space:

```
    ┌─────────────────────────────────────────┐
    │  1. Generate initial pool (1000 random   │
    │     molecules via VAE sampling)          │
    │  2. Encode pool → latent vectors         │
    │  3. Predict Tg for pool                  │
    └──────────────┬──────────────────────────┘
                   ▼
    ┌─────────────────────────────────────────┐
    │  For each iteration (×50):              │
    │    a. Fit GP surrogate on (z, objective) │
    │    b. Generate 1000 random candidates    │
    │    c. Score by Expected Improvement (EI) │
    │    d. Select top 50 candidates           │◄──┐
    │    e. Decode → validate → re-encode      │   │
    │    f. Add valid candidates to training    │───┘
    └──────────────┬──────────────────────────┘
                   ▼
    ┌─────────────────────────────────────────┐
    │  Return all discovered molecules with   │
    │  Tg predictions, iteration info, PCA    │
    └─────────────────────────────────────────┘
```

## Calling the Tool

### Target a specific Tg

```python
result = bayesian_optimize(
    target_tg=450.0,        # Target Tg in Kelvin
    maximize=False,         # Targeting, not maximizing
    num_iterations=50,
    candidates_per_iteration=50,
    pool_size=1000,
    seed=1,
)
```

### Maximize Tg

```python
result = bayesian_optimize(
    maximize=True,          # Find highest possible Tg
    num_iterations=50,
)
```

## Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `target_tg` | float | 373.0 | Target Tg in Kelvin (ignored if `maximize=True`) |
| `maximize` | bool | False | If True, maximize Tg instead of targeting a value |
| `num_iterations` | int | 50 | Number of BO iterations |
| `candidates_per_iteration` | int | 50 | Top candidates selected per iteration |
| `pool_size` | int | 1000 | Size of initial random molecule pool |
| `seed` | int | 1 | Random seed for reproducibility |

## Return Values

| Field | Type | Description |
|-------|------|-------------|
| `acids` | list[str] | Discovered acid SMILES |
| `epoxides` | list[str] | Discovered epoxide SMILES |
| `tg_predicted` | list[float] | Predicted Tg for each discovery (K) |
| `iterations` | list[int] | BO iteration when each molecule was found |
| `pca_coords` | list[list[float]] | 2D PCA coordinates for visualization |
| `best_tg` | float | Tg of the best molecule found |

## Under the Hood

### Objective Function
- **Targeting mode**: Minimize `(Tg_normalized − target_normalized)²`
- **Maximize mode**: Minimize `−Tg_normalized`

### GP Surrogate
- Kernel: Matérn (ν=2.5) with automatic relevance determination
- Noise: α=0.01 (regularization)
- Normalization: y values normalized by the GP

### Expected Improvement (EI)
```
EI(z) = (f_best − μ(z)) · Φ(Z) + σ(z) · φ(Z)
where Z = (f_best − μ(z)) / σ(z)
```

Balances **exploitation** (high predicted performance) with **exploration** (high uncertainty).

## Interpreting Results

### Convergence
Plot `best_tg` across iterations. A flattening curve indicates convergence:
```python
# Check convergence by iteration
for acid, epoxide, tg, it in zip(
    result["acids"], result["epoxides"],
    result["tg_predicted"], result["iterations"]
):
    print(f"Iter {it}: Tg={tg:.1f} K  acid={acid[:30]}...")
```

### Diversity
Check the number of unique acid/epoxide pairs — Bayesian optimization may repeatedly find similar structures near the optimum. Use `search_neighbors` to expand around the best hits.

### Runtime
- `pool_size=1000, num_iterations=50`: ~5–30 minutes on CPU
- Larger pools or more iterations increase runtime roughly linearly
- Runtime is highly variable depending on molecular complexity and decoding success rate
- No need to set explicit timeouts — the server default (4 hours) is generous. Jobs exceeding 4 hours will time out.

## Follow-up Workflow

After optimization, validate top candidates with physics-based MD:

```python
# Best candidates from BO
best_acids = result["acids"][:5]
best_epoxides = result["epoxides"][:5]

# Validate with vitrimer_tg_sim
for acid, epoxide in zip(best_acids, best_epoxides):
    box = build_vitrimer_box(acid_smiles=acid, epoxide_smiles=epoxide)
    run_equilibration(work_dir=box["work_dir"])
    run_tg_production(work_dir=box["work_dir"])
    tg = compute_tg(work_dir=box["work_dir"])
    
    # Calibrate
    cal = calibrate_tg(
        acid_smiles=[acid],
        epoxide_smiles=[epoxide],
        tg_md=[tg["tg_mean"]],
    )
```
