"""Earthscience domain tool definitions.

Exports ``EARTHSCIENCE_TOOLS``, a list of all ``ToolDefinition`` objects.
These are server-side metadata only — implementations live in the
``earthscience_tools`` package installed in the execution environment.
"""

from .definitions import (
    clip_to_geometry,
    compute_ndvi,
    get_item_assets,
    list_collections,
    search_stac_items,
    zonal_statistics,
)

EARTHSCIENCE_TOOLS = [
    list_collections,
    search_stac_items,
    get_item_assets,
    compute_ndvi,
    clip_to_geometry,
    zonal_statistics,
]

__all__ = ["EARTHSCIENCE_TOOLS"]
