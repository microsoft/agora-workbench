from typing import Optional

from idaes.core.solvers import get_solver

from ..builder import IdaesFlowsheetBuilder


def solve_idaes_flowsheet(
    builder: IdaesFlowsheetBuilder, tee: bool = True, solver_options: Optional[dict] = None
) -> dict:
    """
    Solve an IDAES flowsheet model.

    This tool solves the flowsheet using IDAES's default solver (typically IPOPT).
    The model should be built, specified, and initialized before calling this function.

    Args:
        builder: IdaesFlowsheetBuilder instance with a built and initialized model
        tee: Whether to stream solver output to console (default: True)
        solver_options: Optional dictionary of solver options (e.g., {"tol": 1e-6, "max_iter": 500})

    Returns:
        dict containing:
            - "builder": IdaesFlowsheetBuilder with solved model
            - "termination_condition": Solver termination condition as string
            - "success": Boolean indicating if solve was successful
    """
    # Get the IDAES default solver
    solver = get_solver()

    # Apply solver options if provided
    if solver_options:
        solver.options = solver_options

    # Solve the model
    results = solver.solve(builder.model, tee=tee)

    # Extract termination condition
    termination_condition = str(results.solver.termination_condition)

    # Check if solve was successful (optimal or feasible)
    success = termination_condition in ["optimal", "feasible"]

    return {"builder": builder, "termination_condition": termination_condition, "success": success}
