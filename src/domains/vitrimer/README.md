# Vitrimer Domain Servers (VAE + Tg Simulation)

This domain is implemented as two MCP code-execution servers:

1. **`vitrimer_vae`** — AI-guided inverse design of vitrimer monomer pairs (HierVAE + Bayesian optimization).
2. **`vitrimer_tg_sim`** — physics-based Tg estimation via EMC + LAMMPS molecular-dynamics workflow.

Together they support generate → predict → simulate → calibrate workflows.

## Server overview

| Server | Purpose | Main domain tools |
|---|---|---|
| `vitrimer_vae` | Generate and optimize vitrimer candidates in latent space | `sample_molecules`, `predict_tg`, `search_neighbors`, `interpolate_molecules`, `calibrate_tg`, `reconstruct_molecules`, `bayesian_optimize` |
| `vitrimer_tg_sim` | Estimate Tg from MD simulation pipeline | `build_vitrimer_box`, `run_equilibration`, `run_tg_production`, `compute_tg` |

## Dependencies

### `vitrimer_vae`

- Local tools package: `domains/vitrimer_vae/server/tools`
- Key packages: `torch`, `rdkit`, `numpy`, `pandas`, `scipy`, `scikit-learn`, `networkx`, `ipykernel`
- Requirements file: `domains/vitrimer_vae/server/requirements.txt`

### `vitrimer_tg_sim`

- Local tools package: `domains/vitrimer_tg_sim/server/tools`
- Key packages: `rdkit`, `numpy`, `pandas`, `scipy`, `pwlf`, `emc-pypi`, `ipykernel`
- Requirements file: `domains/vitrimer_tg_sim/server/requirements.yaml`
- External runtime tools (container/runtime environment): EMC and LAMMPS for MD stages

## Setup and run

From `AgoraAgentMAF/`:

```bash
# Start VAE server
uv run python -m domains.vitrimer_vae.server.vitrimer_vae_server

# Start Tg simulation server
uv run python -m domains.vitrimer_tg_sim.server.vitrimer_tg_sim_server
```

Default ports are configured in `server_registry.yaml`:

- `vitrimer_tg_sim`: `8010`
- `vitrimer_vae`: `8011`

Ensure `ENTRA_CLIENT_ID` and `ENTRA_TENANT_ID` are configured for server auth.

## Example workflows

- `examples/run_vitrimer_tg_sim.py` — estimate Tg for a reference system with the MD pipeline.
- `examples/run_vitrimer_design_tg350.py` — target-driven design workflow combining VAE generation with Tg simulation validation.
