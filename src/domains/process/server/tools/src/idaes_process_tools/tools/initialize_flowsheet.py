from typing import Optional

from ..builder import IdaesFlowsheetBuilder


def initialize_idaes_flowsheet(
    builder: IdaesFlowsheetBuilder, solver: Optional[str] = None, outlvl: str = "info"
) -> dict[str, IdaesFlowsheetBuilder]:
    """
    Initialize an IDAES flowsheet.

    This tool initializes the flowsheet by calling the builder's initialize_flowsheet method,
    which propagates state through the units in the correct sequence starting from feed units.

    Args:
        builder: IdaesFlowsheetBuilder instance with a built model
        solver: Optional solver to use for initialization (default: None uses IDAES default)
        outlvl: Output level for initialization logging, either "info" or "debug" (default: "info")

    Returns:
        dict with "builder" key containing the IdaesFlowsheetBuilder with initialized model
    """
    # Call the builder's initialization method
    builder.initialize_flowsheet(solver=solver, outlvl=outlvl)

    return {"builder": builder}
