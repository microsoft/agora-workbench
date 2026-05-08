"""Earth science domain state vocabulary.

Defines the canonical state tokens for the earthscience tool graph.
Each token represents a meaningful intermediate artifact that downstream
tools can consume.

Note: like ``domain_examples.chemistry.states``, this module lives under
``domain_examples`` rather than ``domains/`` so it is not auto-discovered
by the default ``StateGraph`` loader. The server registers it via the
``ToolRegistry`` it builds at startup.
"""

from enum import Enum


class EarthscienceState(str, Enum):
    """State tokens for the earthscience domain tool graph.

    The graph flows:

        list_collections / search_stac_items ─► ITEMS_SEARCHED
                                                    │
                                                    ▼
                                          get_item_assets
                                                    │
                                                    ▼
                                            ASSETS_RESOLVED
                                                    │
                                                    ▼
                                              compute_ndvi
                                                    │
                                                    ▼
                                             NDVI_COMPUTED
                                                    │
                                          ┌─────────┴─────────┐
                                          ▼                   ▼
                                  clip_to_geometry    zonal_statistics
                                          │                   │
                                          ▼                   ▼
                                   RASTER_CLIPPED   ZONAL_STATS_COMPUTED
    """

    ITEMS_SEARCHED = "earthscience.items_searched"
    ASSETS_RESOLVED = "earthscience.assets_resolved"
    NDVI_COMPUTED = "earthscience.ndvi_computed"
    RASTER_CLIPPED = "earthscience.raster_clipped"
    ZONAL_STATS_COMPUTED = "earthscience.zonal_stats_computed"


STATE_AFFORDANCES = {
    EarthscienceState.ITEMS_SEARCHED: [
        "find satellite imagery over an AOI",
        "search the Planetary Computer STAC catalog",
        "discover Sentinel-2 or Landsat scenes",
    ],
    EarthscienceState.ASSETS_RESOLVED: [
        "get signed download URLs for raster bands",
        "refresh STAC asset URLs",
    ],
    EarthscienceState.NDVI_COMPUTED: [
        "compute a vegetation index",
        "calculate NDVI",
        "produce a vegetation raster",
    ],
    EarthscienceState.RASTER_CLIPPED: [
        "clip a raster to a polygon",
        "subset imagery to an AOI",
    ],
    EarthscienceState.ZONAL_STATS_COMPUTED: [
        "summarise raster values per polygon",
        "compute mean NDVI per region",
        "compute zonal statistics",
    ],
}
