"""
Run parallel Tg production cooling simulations.

Launches 5 independent LAMMPS cooling runs (800 → 100 K) from the
restart snapshots produced by equilibration. All replicas execute
in parallel via ``concurrent.futures.ProcessPoolExecutor``.
"""

import os
import shutil
from concurrent.futures import ProcessPoolExecutor, as_completed

from vitrimer_tg_sim_tools.lammps_runner import (
    run_lammps,
    write_production_script,
)


def _run_single_replica(
    restart_src: str,
    replica_dir: str,
    replica_id: int,
    timeout: int,
) -> dict:
    """Run a single production replica. Called in a subprocess."""
    try:
        os.makedirs(replica_dir, exist_ok=True)
        restart_dst = os.path.join(replica_dir, "0.restart")
        shutil.copy2(restart_src, restart_dst)

        script = write_production_script(
            restart_file="0.restart",
            replica_id=replica_id,
        )

        output = run_lammps(
            input_script=script,
            work_dir=replica_dir,
            timeout=timeout,
            log_file=f"log.prod.{replica_id}",
        )

        # Count how many step files were produced
        step_files = [f for f in os.listdir(replica_dir) if f.startswith("step_") and f.endswith(".txt")]

        return {
            "replica_id": replica_id,
            "success": True,
            "replica_dir": replica_dir,
            "num_steps": len(step_files),
            "output": output[-1000:],
            "error": None,
        }

    except Exception as e:
        return {
            "replica_id": replica_id,
            "success": False,
            "replica_dir": replica_dir,
            "num_steps": 0,
            "output": "",
            "error": str(e),
        }


def run_tg_production(
    work_dir: str,
    timeout_per_replica: int = 432000,
    max_workers: int = 5,
) -> dict:
    """
    Run 5 parallel Tg production cooling simulations.

    Each replica independently cools from 800 K to 100 K in 10 K steps
    (25 ps NPT ramp + 25 ps NPT hold per step), recording averaged
    density at each temperature. Replicas run in parallel to reduce
    wall-clock time by up to 5×.

    Args:
        work_dir: Directory from a prior ``run_equilibration`` call,
            containing ``eq/restart.1`` through ``eq/restart.5``.
        timeout_per_replica: Max wall time per replica in seconds
            (default 432000 s / 5 days).
        max_workers: Number of parallel LAMMPS processes (default 5).

    Returns:
        Dict with ``success``, ``replica_dirs`` (list of paths to completed
        replica directories), ``num_completed``, ``replica_results``, and ``error``.
    """
    result = {
        "success": False,
        "replica_dirs": [],
        "num_completed": 0,
        "replica_results": [],
        "error": None,
    }

    try:
        eq_dir = os.path.join(work_dir, "eq")
        prod_dir = os.path.join(work_dir, "prod")
        os.makedirs(prod_dir, exist_ok=True)

        # Locate restart files
        restart_files = []
        for i in range(1, 6):
            rpath = os.path.join(eq_dir, f"restart.{i}")
            if os.path.isfile(rpath):
                restart_files.append((i, rpath))

        if len(restart_files) == 0:
            result["error"] = f"No restart files found in {eq_dir}. Run equilibration first."
            return result

        # Launch replicas in parallel
        futures = {}
        with ProcessPoolExecutor(max_workers=max_workers) as executor:
            for replica_id, restart_path in restart_files:
                replica_dir = os.path.join(prod_dir, f"replica_{replica_id}")
                future = executor.submit(
                    _run_single_replica,
                    restart_path,
                    replica_dir,
                    replica_id,
                    timeout_per_replica,
                )
                futures[future] = replica_id

            # Collect results
            for future in as_completed(futures):
                rep_result = future.result()
                result["replica_results"].append(rep_result)
                if rep_result["success"]:
                    result["replica_dirs"].append(rep_result["replica_dir"])
                    result["num_completed"] += 1

        if result["num_completed"] == 0:
            errors = [f"Replica {r['replica_id']}: {r['error']}" for r in result["replica_results"] if r["error"]]
            result["error"] = "All replicas failed.\n" + "\n".join(errors)
            return result

        result["success"] = True
        if result["num_completed"] < len(restart_files):
            failed = [r for r in result["replica_results"] if not r["success"]]
            result["error"] = (
                f"{result['num_completed']}/{len(restart_files)} replicas "
                f"completed. Failed: {[r['replica_id'] for r in failed]}"
            )

    except Exception as e:
        result["error"] = str(e)

    return result
