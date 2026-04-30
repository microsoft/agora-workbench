"""
PyPSA Optimal Power Flow (OPF) Tool.

This module provides functions to run optimal power flow optimization on PyPSA
networks using the HiGHS solver with configurable options.
"""

from pathlib import Path


def run_opf(network_path: str) -> dict:
    """
    Run an optimal power flow (OPF) on a PyPSA network loaded from file using the HiGHS solver.

    This function loads a PyPSA network from a NetCDF file, configures and executes
    an optimization, and returns the results.

    Parameters
    ----------
    network_path : str
        Path to a PyPSA network file (NetCDF .nc format).

    Returns
    -------
    dict
        Dictionary containing:
        - network: pypsa.Network - The optimized network object (if successful)
        - success: bool - Whether optimization completed successfully
        - status: str - Solver status message
        - objective: float - Objective function value (if successful)
        - error: str - Error message (if failed)

    Notes
    -----
    The solver configuration is fixed to use the HiGHS solver with the PDLP
    algorithm and a set of numeric tolerances and performance-related
    options. If different settings are required, this function must be
    adapted or a separate, configurable entry point should be used.

    Only NetCDF (.nc) format is supported.  Pickle-based network files are
    not accepted because Python's ``pickle`` module can execute arbitrary
    code during deserialization, posing a critical security risk.
    """
    SOLVER_NAME = "highs"
    SOLVER_OPTIONS = {
        "threads": 4,
        "solver": "pdlp",
        "run_crossover": "off",
        "small_matrix_value": 1e-6,
        "large_matrix_value": 1e9,
        "primal_feasibility_tolerance": 1e-5,
        "dual_feasibility_tolerance": 1e-5,
        "ipm_optimality_tolerance": 1e-4,
        "parallel": "on",
        "random_seed": 123,
    }

    result = {
        "success": False,
        "network": None,
        "status": None,
        "objective": None,
        "error": None,
    }

    try:
        # Import pypsa inside the function (only available in isolated environment)
        import pypsa

        # Load network from file
        path = Path(network_path)
        if not path.exists():
            result["error"] = f"Network file not found: {network_path}"
            return result

        # Load based on file extension
        if path.suffix == ".nc":
            n = pypsa.Network(network_path)
        else:
            result["error"] = f"Unsupported file format: {path.suffix}. Use .nc (NetCDF)"
            return result

        # Run optimization
        status = n.optimize(solver_name=SOLVER_NAME, solver_options=SOLVER_OPTIONS)

        result["status"] = status[0]
        result["success"] = status[0] == "ok"

        if result["success"]:
            result["network"] = n
            result["objective"] = float(n.objective)
        else:
            result["error"] = f"Optimization failed with status: {status[0]}"

        return result

    except Exception as e:
        result["error"] = f"Failed to run OPF: {str(e)}"
        return result
