---
name: vitrimer-tg-estimation
description: Estimate the glass transition temperature (Tg) of vitrimer polymers via molecular dynamics simulation using EMC box construction, LAMMPS equilibration, parallel production cooling, and bilinear fitting.
parent_skill: vitrimer
---

# Vitrimer Tg Estimation

Use this skill when the user wants to estimate the glass transition temperature of a vitrimer polymer from its monomer SMILES, or when they need to validate ML-predicted Tg values with physics-based MD simulation.

## Sub-Skills

| Sub-skill | When to use |
|-----------|-------------|
| `build-box` | Construct the initial simulation box from acid + epoxide SMILES using EMC and the PCFF force field |
| `equilibration` | Run the LAMMPS equilibration protocol (minimize, relax, anneal) and generate restart snapshots |
| `production` | Run parallel cooling simulations from 800 K → 100 K to collect density–temperature data |
| `tg-analysis` | Compute Tg from density–temperature profiles via bilinear regression |

## Typical Workflow

An end-to-end Tg estimation follows these steps in order:

1. **Build** — load `build-box` to create the simulation box from SMILES
2. **Equilibrate** — load `equilibration` to anneal the system and produce 5 independent snapshots
3. **Produce** — load `production` to run 5 parallel cooling simulations
4. **Analyze** — load `tg-analysis` to fit bilinear models and compute the final Tg

```
acid_smiles + epoxide_smiles
        │
        ▼
  build_vitrimer_box  →  polymer.data + polymer.params
        │
        ▼
  run_equilibration   →  5 × restart snapshots at 800 K
        │
        ▼
  run_tg_production   →  5 × density(T) profiles  [parallel]
        │
        ▼
  compute_tg          →  Tg_mean ± σ (K)
```

## Key Parameters

| Parameter | Default | Notes |
|-----------|---------|-------|
| Density | 0.5 g/cm³ | Initial box density; realistic density emerges after annealing |
| ntotal | 4000 | Target atom count (~4 chains of ~1000 atoms each) |
| Force field | PCFF | Polymer Consistent Force Field via EMC |
| Cooling range | 800 → 100 K | In 10 K steps |
| Replicas | 5 | Independent snapshots, run in parallel |
| Timestep | 0.5 fs | Real units |

## Integration with vitrimer_vae

The `vitrimer_tg_sim` tools complement the `vitrimer_vae` domain:

- **vitrimer_vae** provides fast ML-based Tg prediction (~seconds) and candidate generation
- **vitrimer_tg_sim** provides physics-based MD validation (~hours) for top candidates
- Use `calibrate_tg` from vitrimer_vae to map MD-computed Tg onto the experimental scale

A typical active-learning loop:
1. Generate candidates with `sample_molecules` or `bayesian_optimize` (vitrimer_vae)
2. Validate top picks with the full MD pipeline (vitrimer_tg_sim)
3. Calibrate results with `calibrate_tg` (vitrimer_vae)
4. Refine the search with `search_neighbors` around the best hits

## Limitations

- The protocol targets **<10 compounds** per run; it is not designed for high-throughput screening
- Wall time is dominated by LAMMPS production runs (~1–4 hours per compound depending on system size)
- SMILES must include `*` connection points for polymerization
- PCFF may not parameterize all atom types; exotic chemistries may fail at the `build_vitrimer_box` step
