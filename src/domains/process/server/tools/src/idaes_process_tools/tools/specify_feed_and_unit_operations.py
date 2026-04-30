from ..builder import IdaesFlowsheetBuilder


def specify_feed_and_unit_operations(builder: IdaesFlowsheetBuilder) -> dict[str, IdaesFlowsheetBuilder]:
    """
    Specify feed conditions and unit operations for an IDAES flowsheet.

    This tool applies specifications to the flowsheet by:
    1. Applying stream specifications to feed blocks
    2. Applying unit operation specifications via the variable manager

    Args:
        builder: IdaesFlowsheetBuilder instance with a built model

    Returns:
        dict with "builder" key containing the IdaesFlowsheetBuilder with specified model
    """
    # Call the builder's specification method
    builder.specify_feed_and_units()

    return {"builder": builder}
