---
name: production
description: Run 5 parallel LAMMPS cooling simulations from 800 K to 100 K to collect density–temperature data for Tg determination.
parent_skill: vitrimer-tg-estimation
---

# Production Cooling Runs

Use this skill after `run_equilibration` to perform the Tg cooling protocol. This is the most computationally intensive step, but parallelization reduces wall time by up to 5×.

## Protocol

Each of the 5 replicas independently cools from 800 K to 100 K:

```
800 K ──ramp──▶ 790 K ──hold──▶ 790 K ──ramp──▶ 780 K ──hold──▶ ...  ──▶ 100 K
       25 ps          25 ps           25 ps          25 ps
```

- **71 temperature points**: 800, 790, 780, ..., 110, 100 K
- **Ramp phase** (25 ps): NPT cooling between adjacent temperatures
- **Hold phase** (25 ps): NPT at constant temperature; density averaged via `fix ave/time`
- **Total per replica**: ~3500 ps of simulation time

### Parallelization

All 5 replicas run as independent LAMMPS subprocesses via `ProcessPoolExecutor`:

```
          ┌─── Replica 1 (restart.1) ───┐
          ├─── Replica 2 (restart.2) ───┤
work_dir ─┼─── Replica 3 (restart.3) ───┼─▶ 5 × density(T) profiles
          ├─── Replica 4 (restart.4) ───┤
          └─── Replica 5 (restart.5) ───┘
                    ∥  parallel  ∥
```

Wall time ≈ time for 1 replica (not 5×).

## Calling the Tool

```python
result = run_tg_production(
    work_dir="/path/from/build_vitrimer_box",
    max_workers=5,
)
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `work_dir` | str | *required* | Directory containing `eq/restart.*` files from `run_equilibration` |
| `timeout_per_replica` | int | 432000 | Max wall time per replica in seconds. Runtimes vary: ~2 h for ~1000 atoms, ~6–12 h for ~4000 atoms. Jobs exceeding 5 days will time out. |
| `max_workers` | int | 5 | Number of parallel LAMMPS processes |

### Return Values

| Field | Type | Description |
|-------|------|-------------|
| `success` | bool | Whether at least one replica completed |
| `replica_dirs` | list | Paths to completed replica directories |
| `num_completed` | int | Number of successfully completed replicas |
| `replica_results` | list | Per-replica status (success, num_steps, errors) |
| `error` | str | Error or warning message |

## Output Files

Each replica directory (`prod/replica_N/`) contains:

- `step_1.txt` through `step_70.txt` — density averages at each cooling step
- `log.prod.N` — LAMMPS log file
- `input.lammps` — the input script that was executed

The `step_*.txt` files have the format (from LAMMPS `fix ave/time`):

```
# TimeStep  c_thermo_temp  c_thermo_press  v_sysdensity  v_etotal1
50000        789.95         1.02            0.9823        -12345.6
```

## Resource Considerations

### CPU usage
With `max_workers=5`, all 5 replicas run simultaneously. On a machine with fewer than 5 cores, reduce `max_workers` to avoid oversubscription:

```python
result = run_tg_production(work_dir=wd, max_workers=2)
```

### Partial completion
The tool succeeds if **at least 1 replica** completes. Check `num_completed` and the `error` field for warnings about failed replicas. Even 3–4 replicas give reasonable statistics for Tg.

### Timeout guidance
- Small systems (~2000 atoms): ~30–60 min per replica
- Medium systems (~4000 atoms): ~1–3 hours per replica
- Large systems (~8000 atoms): ~3–6 hours per replica

## What Happens Next

Pass the same `work_dir` to `compute_tg`:

```python
tg_result = compute_tg(work_dir=result["work_dir"])
```
