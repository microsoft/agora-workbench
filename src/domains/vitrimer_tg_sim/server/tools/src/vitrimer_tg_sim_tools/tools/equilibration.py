"""
Run LAMMPS equilibration for a vitrimer system.

Performs the full equilibration protocol: energy minimization, NVT/NPT
relaxation, annealing to 800 K, and generation of 5 independent restart
snapshots for production runs.
"""

import os
import shutil

from vitrimer_tg_sim_tools.lammps_runner import (
    run_lammps,
    write_equilibration_script,
)


def run_equilibration(
    work_dir: str,
    timeout: int = 432000,
) -> dict:
    """
    Run the vitrimer equilibration protocol in LAMMPS.

    Expects ``polymer.data`` and ``polymer.params`` in *work_dir*
    (produced by ``build_vitrimer_box``).

    Protocol:
      1. Conjugate-gradient energy minimization
      2. NVT relaxation at 300 K for 50 ps
      3. NPT relaxation at 300 K / 1 atm for 100 ps
      4. NPT heating from 300 K → 800 K over 500 ps
      5. NPT hold at 800 K for 50 ps, writing 5 restart snapshots
         at 10 ps intervals

    Args:
        work_dir: Directory containing ``polymer.data`` and ``polymer.params``
            from a prior ``build_vitrimer_box`` call.
        timeout: Maximum wall-clock time in seconds (default 432000 s / 5 days).

    Returns:
        Dict with ``success``, ``restart_files`` (list of 5 paths),
        ``lammps_output`` (last 2000 chars), and ``error``.
    """
    result = {
        "success": False,
        "restart_files": [],
        "lammps_output": "",
        "error": None,
    }

    try:
        data_path = os.path.join(work_dir, "polymer.data")
        params_path = os.path.join(work_dir, "polymer.params")

        if not os.path.isfile(data_path):
            result["error"] = f"polymer.data not found in {work_dir}"
            return result
        if not os.path.isfile(params_path):
            result["error"] = f"polymer.params not found in {work_dir}"
            return result

        # Create equilibration subdirectory
        eq_dir = os.path.join(work_dir, "eq")
        os.makedirs(eq_dir, exist_ok=True)

        # Copy data/params into eq dir
        shutil.copy2(data_path, os.path.join(eq_dir, "polymer.data"))
        shutil.copy2(params_path, os.path.join(eq_dir, "polymer.params"))

        # Generate and run equilibration script
        script = write_equilibration_script(
            data_file="polymer.data",
            params_file="polymer.params",
        )

        output = run_lammps(
            input_script=script,
            work_dir=eq_dir,
            timeout=timeout,
            log_file="log.eq",
        )

        # Verify restart files were created
        restart_files = []
        for i in range(1, 6):
            rpath = os.path.join(eq_dir, f"restart.{i}")
            if os.path.isfile(rpath):
                restart_files.append(rpath)

        if len(restart_files) < 5:
            result["error"] = (
                f"Expected 5 restart files, found {len(restart_files)}. Equilibration may have failed or timed out."
            )
            result["lammps_output"] = output[-2000:]
            result["restart_files"] = restart_files
            return result

        result["success"] = True
        result["restart_files"] = restart_files
        result["lammps_output"] = output[-2000:]

    except TimeoutError as e:
        result["error"] = str(e)
    except Exception as e:
        result["error"] = str(e)

    return result
