---
name: equilibration
description: Run the LAMMPS equilibration protocol to minimize, relax, anneal, and generate 5 independent restart snapshots for production cooling runs.
parent_skill: vitrimer-tg-estimation
---

# Equilibration

Use this skill after `build_vitrimer_box` to prepare the vitrimer system for Tg production runs. The equilibration removes local heterogeneities from the EMC-generated structure and brings the system to a realistic density.

## Protocol

The `run_equilibration` tool executes the following steps in a single LAMMPS run:

| Step | Ensemble | Temperature | Duration | Purpose |
|------|----------|-------------|----------|---------|
| 1 | — | — | — | Conjugate-gradient energy minimization |
| 2 | NVT | 300 K | 50 ps | Relax at constant volume |
| 3 | NPT | 300 K, 1 atm | 100 ps | Relax at constant pressure (density adjusts) |
| 4 | NPT | 300 → 800 K, 1 atm | 500 ps | Anneal: heat to above expected Tg |
| 5 | NPT | 800 K, 1 atm | 50 ps | Hold at 800 K, write 5 restart snapshots at 10 ps intervals |

The 5 restart snapshots are separated by 10 ps, which is sufficient to eliminate correlation between replicas and ensure independent sampling for production runs.

## Calling the Tool

```python
result = run_equilibration(
    work_dir="/path/from/build_vitrimer_box",
    timeout=7200,  # 2 hours max
)
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `work_dir` | str | *required* | Directory with `polymer.data` and `polymer.params` from `build_vitrimer_box` |
| `timeout` | int | 7200 | Max wall-clock time in seconds |

### Return Values

| Field | Type | Description |
|-------|------|-------------|
| `success` | bool | Whether equilibration completed |
| `restart_files` | list | Paths to 5 restart files (`eq/restart.1` through `eq/restart.5`) |
| `lammps_output` | str | Last 2000 chars of LAMMPS log (for diagnostics) |
| `error` | str | Error message if failed |

## Force Field Details

The equilibration uses the PCFF class2 force field:

```
pair_style      lj/class2/coul/long 9.5 9.5
bond_style      class2
angle_style     class2
dihedral_style  class2
improper_style  class2
pair_modify     mix sixthpower tail yes
special_bonds   lj/coul 0 0 1
kspace_style    pppm/cg 0.001
```

Timestep is 0.5 fs (real units), consistent with the publication protocol.

## Common Issues

### Timeout
Equilibration typically takes 30–90 minutes depending on system size. If it times out:
- Check `lammps_output` in the result for where it stopped
- Increase `timeout` if the run was making progress
- Reduce `ntotal` in the `build_vitrimer_box` step for smaller systems

### "polymer.data not found"
The `work_dir` doesn't contain the expected files. Ensure you're passing the `work_dir` from a successful `build_vitrimer_box` call.

### LAMMPS crashes during minimization
The initial EMC structure may have overlapping atoms. This is rare but can happen with large monomers. Try rebuilding the box with a different `seed`.

## What Happens Next

Pass the same `work_dir` to `run_tg_production`:

```python
prod_result = run_tg_production(work_dir=result["work_dir"])
```

The production tool will find the restart files in `eq/restart.1` through `eq/restart.5`.
