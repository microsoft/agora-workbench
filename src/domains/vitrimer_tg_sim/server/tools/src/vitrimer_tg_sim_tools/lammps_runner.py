"""
LAMMPS execution helpers for vitrimer Tg simulations.

Provides functions for generating LAMMPS input scripts and running
LAMMPS as a subprocess. Shared by equilibration and production tools.
"""

import logging
import os
import subprocess
import shutil
from typing import Optional

LOGGER = logging.getLogger(__name__)

# Default LAMMPS binary — overridable via environment variable
LAMMPS_CMD = os.environ.get("LAMMPS_CMD", "lmp")


def _find_lammps() -> str:
    """Locate the LAMMPS executable."""
    cmd = LAMMPS_CMD
    if shutil.which(cmd):
        return cmd
    # Try common alternatives
    for alt in ["lmp_serial", "lmp_mpi", "lammps"]:
        if shutil.which(alt):
            return alt
    raise RuntimeError("LAMMPS executable not found. Set LAMMPS_CMD environment variable or ensure 'lmp' is on PATH.")


def run_lammps(
    input_script: str,
    work_dir: str,
    timeout: Optional[int] = None,
    log_file: str = "log.lammps",
) -> str:
    """
    Run a LAMMPS simulation.

    Args:
        input_script: Content of the LAMMPS input script.
        work_dir: Working directory for the simulation.
        timeout: Max runtime in seconds (None = no limit).
        log_file: Name of the LAMMPS log file.

    Returns:
        Combined stdout+stderr from LAMMPS.

    Raises:
        RuntimeError: If LAMMPS exits with non-zero status.
        TimeoutError: If simulation exceeds timeout.
    """
    lmp = _find_lammps()
    os.makedirs(work_dir, exist_ok=True)

    input_path = os.path.join(work_dir, "input.lammps")
    with open(input_path, "w") as f:
        f.write(input_script)

    try:
        result = subprocess.run(
            [lmp, "-in", "input.lammps", "-log", log_file],
            capture_output=True,
            text=True,
            cwd=work_dir,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise TimeoutError(f"LAMMPS timed out after {timeout}s in {work_dir}") from e

    output = result.stdout + result.stderr

    if result.returncode != 0:
        # Include last portion of output for diagnostics
        raise RuntimeError(
            f"LAMMPS exited with code {result.returncode} in {work_dir}.\nOutput (last 3000 chars):\n{output[-3000:]}"
        )

    return output


def write_equilibration_script(
    data_file: str = "polymer.data",
    params_file: str = "polymer.params",
) -> str:
    """
    Generate a LAMMPS input script for the equilibration protocol.

    Protocol (from publication):
      1. Energy minimization (conjugate gradient)
      2. NVT relaxation at 300 K for 50 ps
      3. NPT relaxation at 300 K, 1 atm for 100 ps
      4. NPT heating from 300 K to 800 K over 500 ps
      5. NPT hold at 800 K for 50 ps, writing 5 restart files
         at 10 ps intervals (for independent production replicas)

    Timestep: 0.5 fs (real units)
    """
    dt = 0.5  # fs
    steps_per_ps = int(1000.0 / dt)  # 2000 steps/ps

    nvt_steps = 50 * steps_per_ps  # 50 ps
    npt_relax_steps = 100 * steps_per_ps  # 100 ps
    heat_steps = 500 * steps_per_ps  # 500 ps
    snapshot_interval = 10 * steps_per_ps  # 10 ps between restarts

    lines = []

    # --- Header ---
    lines.append("# Vitrimer Tg equilibration protocol")
    lines.append("# PCFF force field, real units")
    lines.append("")
    lines.append("units           real")
    lines.append("boundary        p p p")
    lines.append("atom_style      full")
    lines.append("neighbor        2.0 bin")
    lines.append("neigh_modify    delay 0 every 1 check yes")
    lines.append("")

    # --- Force field ---
    lines.append("pair_style      lj/class2/coul/long 9.5 9.5")
    lines.append("bond_style      class2")
    lines.append("angle_style     class2")
    lines.append("dihedral_style  class2")
    lines.append("improper_style  class2")
    lines.append("pair_modify     mix sixthpower tail yes")
    lines.append("special_bonds   lj/coul 0 0 1")
    lines.append("kspace_style    pppm/cg 0.001")
    lines.append("")

    # --- Read data ---
    lines.append(f"read_data       {data_file}")
    lines.append(f"include         {params_file}")
    lines.append("")

    # --- Variables ---
    lines.append("variable        sysvol    equal vol")
    lines.append("variable        sysmass   equal mass(all)/6.0221367e+23")
    lines.append("variable        sysdensity equal v_sysmass/v_sysvol/1.0e-24")
    lines.append("")

    # --- Thermo output ---
    lines.append("thermo_style    custom step temp press vol v_sysdensity etotal")
    lines.append("thermo          10000")
    lines.append("")

    # --- 1. Energy minimization ---
    lines.append("# Step 1: Conjugate gradient minimization")
    lines.append("minimize        1.0e-4 1.0e-6 10000 100000")
    lines.append("")

    # --- 2. NVT relaxation at 300 K ---
    lines.append("# Step 2: NVT relaxation at 300 K for 50 ps")
    lines.append(f"timestep        {dt}")
    lines.append("velocity        all create 300.0 12345 dist gaussian")
    lines.append("fix             eq_nvt all nvt temp 300 300 100")
    lines.append(f"run             {nvt_steps}")
    lines.append("unfix           eq_nvt")
    lines.append("")

    # --- 3. NPT relaxation at 300 K, 1 atm ---
    lines.append("# Step 3: NPT relaxation at 300 K, 1 atm for 100 ps")
    lines.append("fix             eq_npt all npt temp 300 300 100 iso 1 1 1000")
    lines.append(f"run             {npt_relax_steps}")
    lines.append("unfix           eq_npt")
    lines.append("")

    # --- 4. Heat from 300 K to 800 K ---
    lines.append("# Step 4: NPT heating 300 -> 800 K over 500 ps")
    lines.append("fix             heat all npt temp 300 800 100 iso 1 1 1000")
    lines.append(f"run             {heat_steps}")
    lines.append("unfix           heat")
    lines.append("")

    # --- 5. Hold at 800 K, write 5 restart snapshots ---
    lines.append("# Step 5: Hold at 800 K for 50 ps, write 5 restart snapshots")
    lines.append("fix             hold all npt temp 800 800 100 iso 1 1 1000")
    for i in range(1, 6):
        lines.append(f"run             {snapshot_interval}")
        lines.append(f"write_restart   restart.{i}")
    lines.append("unfix           hold")
    lines.append("")
    lines.append('print "Equilibration complete."')

    return "\n".join(lines) + "\n"


def write_production_script(
    restart_file: str = "0.restart",
    replica_id: int = 1,
) -> str:
    """
    Generate a LAMMPS input script for the Tg production cooling run.

    Protocol (from publication):
      - Cool from 800 K to 100 K in 10 K steps
      - Each step: 25 ps NPT ramp + 25 ps NPT hold at constant T
      - During hold, density averaged from 25 frames via ave/time
      - Timestep: 0.5 fs

    This produces a data.txt file with temperature and density at each
    cooling step (71 data points: 800, 790, ..., 100 K).

    Args:
        restart_file: Name of the restart file to read.
        replica_id: Replica identifier (for logging).

    Returns:
        LAMMPS input script as a string.
    """
    dt = 0.5  # fs
    steps_per_ps = int(1000.0 / dt)  # 2000 steps/ps
    ramp_steps = 25 * steps_per_ps  # 25 ps = 50000 steps
    hold_steps = 25 * steps_per_ps  # 25 ps = 50000 steps

    t_max = 800
    t_min = 100
    dt_temp = 10  # K per step

    lines = []

    # --- Header ---
    lines.append(f"# Vitrimer Tg production cooling - replica {replica_id}")
    lines.append("# PCFF force field, real units")
    lines.append("")
    lines.append("units           real")
    lines.append("boundary        p p p")
    lines.append("atom_style      full")
    lines.append("neighbor        2.0 bin")
    lines.append("neigh_modify    delay 0 every 1 check yes")
    lines.append("")

    # --- Force field ---
    lines.append("pair_style      lj/class2/coul/long 9.5 9.5")
    lines.append("bond_style      class2")
    lines.append("angle_style     class2")
    lines.append("dihedral_style  class2")
    lines.append("improper_style  class2")
    lines.append("pair_modify     mix sixthpower tail yes")
    lines.append("special_bonds   lj/coul 0 0 1")
    lines.append("kspace_style    pppm/cg 0.001")
    lines.append(f"read_restart    {restart_file}")
    lines.append("")

    # --- Variables ---
    lines.append("variable        sysvol    equal vol")
    lines.append("variable        sysmass   equal mass(all)/6.0221367e+23")
    lines.append("variable        sysdensity equal v_sysmass/v_sysvol/1.0e-24")
    lines.append("variable        etotal1   equal etotal")
    lines.append("")

    # --- Cooling loop ---
    # Generate alternating ramp / hold segments from T_max down to T_min
    step_count = 0
    temperatures = list(range(t_max, t_min - 1, -dt_temp))  # 800, 790, ..., 100

    for i in range(len(temperatures) - 1):
        t_start = temperatures[i]
        t_end = temperatures[i + 1]
        step_count += 1

        # Ramp segment: cool from t_start to t_end
        lines.append(f"# Cooling: {t_start} K -> {t_end} K (ramp)")
        lines.append("reset_timestep  0")
        lines.append("thermo_style    custom cpu step time temp press vol v_sysdensity etotal lx ly lz")
        lines.append("thermo          10000")
        lines.append(f"fix             cool all npt temp {t_start} {t_end} 100 iso 1 1 1000")
        lines.append(f"timestep        {dt}")
        lines.append(f"run             {ramp_steps}")
        lines.append("unfix           cool")
        lines.append("")

        # Hold segment: hold at t_end, record density
        lines.append(f"# Hold at {t_end} K, record density")
        lines.append("reset_timestep  0")
        lines.append("thermo_style    custom cpu step time temp press vol v_sysdensity etotal lx ly lz")
        lines.append("thermo          10000")
        lines.append(f"fix             hold all npt temp {t_end} {t_end} 100 iso 1 1 1000")
        lines.append(
            "fix             avg all ave/time 100 500 50000 "
            "c_thermo_temp c_thermo_press v_sysdensity v_etotal1 "
            f"file step_{step_count}.txt"
        )
        lines.append(f"timestep        {dt}")
        lines.append(f"run             {hold_steps}")
        lines.append("unfix           hold")
        lines.append("unfix           avg")
        lines.append("")

    lines.append(f'print "Production replica {replica_id} complete."')

    return "\n".join(lines) + "\n"


def parse_density_temperature(work_dir: str) -> tuple[list[float], list[float]]:
    """
    Parse temperature and density from production run output files.

    Reads step_N.txt files written by the ave/time fix during hold phases.
    Each file contains one averaged data point.

    Args:
        work_dir: Directory containing step_*.txt files.

    Returns:
        Tuple of (temperatures, densities) lists, ordered from high to low T.
    """
    temperatures = []
    densities = []

    # Find all step files and sort by index
    step_files = []
    for fname in os.listdir(work_dir):
        if fname.startswith("step_") and fname.endswith(".txt"):
            idx = int(fname.replace("step_", "").replace(".txt", ""))
            step_files.append((idx, fname))

    step_files.sort(key=lambda x: x[0])

    for _, fname in step_files:
        fpath = os.path.join(work_dir, fname)
        with open(fpath) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                values = line.split()
                if len(values) >= 4:
                    # Columns: timestep, temperature, pressure, density, [etotal]
                    temp = float(values[1])
                    dens = float(values[3])
                    temperatures.append(temp)
                    densities.append(dens)
                    break  # Only one data line per file from ave/time

    return temperatures, densities
