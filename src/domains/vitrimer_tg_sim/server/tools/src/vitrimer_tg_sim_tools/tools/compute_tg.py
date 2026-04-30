"""
Compute glass transition temperature (Tg) from production simulation data.

Parses density–temperature profiles from the cooling runs, fits a
bilinear (piecewise-linear, 2-segment) regression to each replica,
and averages the intersection points to estimate Tg.
"""

import os

import numpy as np
import pwlf

from vitrimer_tg_sim_tools.lammps_runner import parse_density_temperature


def _fit_bilinear_tg(
    temperatures: list[float],
    densities: list[float],
) -> float:
    """
    Fit a two-segment piecewise linear model to density vs temperature.

    Returns the breakpoint (intersection) which is defined as Tg.
    """
    t_arr = np.array(temperatures, dtype=np.float64)
    d_arr = np.array(densities, dtype=np.float64)

    model = pwlf.PiecewiseLinFit(t_arr, d_arr)
    breakpoints = model.fit(2)
    # breakpoints = [T_min, Tg, T_max]
    tg = breakpoints[1]
    return float(tg)


def compute_tg(
    work_dir: str,
) -> dict:
    """
    Compute Tg from density–temperature data across production replicas.

    Reads the step output files from each completed replica directory
    under ``work_dir/prod/``, fits a bilinear model to each, and
    reports the mean Tg and coefficient of variation.

    Args:
        work_dir: Top-level simulation directory containing ``prod/replica_N/``
            subdirectories from a prior ``run_tg_production`` call.

    Returns:
        Dict with ``tg_mean`` (K), ``tg_std`` (K), ``tg_cv``
        (coefficient of variation), ``tg_per_replica`` (list),
        ``num_replicas``, ``success``, and ``error``.
    """
    result = {
        "success": False,
        "tg_mean": None,
        "tg_std": None,
        "tg_cv": None,
        "tg_per_replica": [],
        "num_replicas": 0,
        "density_temperature_summary": [],
        "error": None,
    }

    try:
        prod_dir = os.path.join(work_dir, "prod")
        if not os.path.isdir(prod_dir):
            result["error"] = f"Production directory not found: {prod_dir}"
            return result

        # Find replica directories
        replica_dirs = sorted(
            [
                os.path.join(prod_dir, d)
                for d in os.listdir(prod_dir)
                if d.startswith("replica_") and os.path.isdir(os.path.join(prod_dir, d))
            ]
        )

        if not replica_dirs:
            result["error"] = f"No replica directories found in {prod_dir}"
            return result

        tg_values = []
        for rdir in replica_dirs:
            temps, densities = parse_density_temperature(rdir)

            if len(temps) < 10:
                # Not enough data points for a meaningful fit
                continue

            try:
                tg = _fit_bilinear_tg(temps, densities)
                if tg > 0:
                    tg_values.append(tg)
                    result["density_temperature_summary"].append(
                        {
                            "replica": os.path.basename(rdir),
                            "tg": round(tg, 2),
                            "num_points": len(temps),
                            "temp_range": [round(min(temps), 1), round(max(temps), 1)],
                        }
                    )
            except Exception as e:
                result["density_temperature_summary"].append(
                    {
                        "replica": os.path.basename(rdir),
                        "tg": None,
                        "error": str(e),
                    }
                )

        if not tg_values:
            result["error"] = "No valid Tg values obtained from any replica."
            return result

        tg_arr = np.array(tg_values)
        tg_mean = float(np.mean(tg_arr))
        tg_std = float(np.std(tg_arr))
        tg_cv = float(tg_std / tg_mean) if tg_mean > 0 else float("nan")

        result["success"] = True
        result["tg_mean"] = round(tg_mean, 2)
        result["tg_std"] = round(tg_std, 2)
        result["tg_cv"] = round(tg_cv, 4)
        result["tg_per_replica"] = [round(v, 2) for v in tg_values]
        result["num_replicas"] = len(tg_values)

    except Exception as e:
        result["error"] = str(e)

    return result
