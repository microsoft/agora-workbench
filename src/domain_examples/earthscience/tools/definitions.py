"""
Tool definitions (metadata) for the earthscience domain.

This module contains only ``ToolDefinition`` objects — server-side schemas,
state transitions, and affordances. Implementations live in the
``earthscience_tools`` pip package, which is installed into the execution
environment at build time.

The ``module`` field on each definition points to the installed package
(e.g. ``earthscience_tools.search_stac_items``), ensuring the kernel's
lazy ``from {module} import {name}`` import resolves correctly.
"""

from code_execution import ReturnSpec, StateTransition, ToolDefinition, ToolParameter

# ============================================================================
# Low complexity: STAC discovery (no raster I/O)
# ============================================================================

list_collections = ToolDefinition(
    name="list_collections",
    description=(
        "List Microsoft Planetary Computer STAC collections, optionally "
        "filtered by a substring matched against id/title/description. "
        "Use this when the user is unsure which collection to query."
    ),
    required_parameters=[],
    optional_parameters=[
        ToolParameter(
            name="search",
            type=str,
            description="Optional case-insensitive substring filter.",
            default=None,
        ),
        ToolParameter(
            name="max_results",
            type=int,
            description="Cap on returned collections (default 50).",
            default=50,
        ),
    ],
    return_spec=[
        ReturnSpec(name="num_total", type=int, description="Collections matched after filtering."),
        ReturnSpec(name="num_returned", type=int, description="Collections returned (capped by max_results)."),
        ReturnSpec(
            name="collections",
            type=list,
            description="List of {id, title, description, spatial_extent, temporal_extent, license, keywords}.",
        ),
    ],
    module="earthscience_tools.list_collections",
    state_transition=StateTransition(),
    affordances=[
        "list available datasets",
        "find a STAC collection",
        "discover Planetary Computer collections",
    ],
)

search_stac_items = ToolDefinition(
    name="search_stac_items",
    description=(
        "Search a Planetary Computer STAC collection for items matching "
        "an area, time range, and optional cloud-cover threshold. Returns "
        "items with signed asset URLs (short-lived). Typically the first "
        "raster-discovery step."
    ),
    required_parameters=[
        ToolParameter(
            name="collection",
            type=str,
            description='Collection ID (e.g. "sentinel-2-l2a", "landsat-c2-l2", "cop-dem-glo-30").',
        ),
    ],
    optional_parameters=[
        ToolParameter(
            name="bbox",
            type=list,
            description="Bounding box [west, south, east, north] in EPSG:4326.",
            default=None,
        ),
        ToolParameter(
            name="datetime",
            type=str,
            description='Date or range ("2024-06-01" or "2024-06-01/2024-06-30").',
            default=None,
        ),
        ToolParameter(
            name="cloud_cover_lt",
            type=float,
            description="Filter to items with eo:cloud_cover < this percentage.",
            default=None,
        ),
        ToolParameter(
            name="query",
            type=dict,
            description='Additional STAC query-extension filters (e.g. {"platform": {"eq": "sentinel-2a"}}).',
            default=None,
        ),
        ToolParameter(
            name="max_items",
            type=int,
            description="Cap on items returned (default 25, hard limit 200).",
            default=25,
        ),
    ],
    return_spec=[
        ReturnSpec(name="num_items", type=int, description="Number of items returned."),
        ReturnSpec(name="collection", type=str, description="Collection searched."),
        ReturnSpec(
            name="items",
            type=list,
            description="Per-item dict with id, datetime, bbox, cloud_cover, platform, assets (key→signed URL).",
        ),
    ],
    module="earthscience_tools.search_stac_items",
    state_transition=StateTransition(
        produces=frozenset({"earthscience.items_searched"}),
    ),
    affordances=[
        "find satellite imagery over an AOI",
        "search Sentinel-2 or Landsat scenes",
        "query the Planetary Computer STAC API",
        "filter imagery by cloud cover",
    ],
)

get_item_assets = ToolDefinition(
    name="get_item_assets",
    description=(
        "Fetch a single STAC item by collection + item_id and return its "
        "signed asset URLs. Use this to refresh URLs (signed links expire "
        "after ~1 hour) or to inspect a known item without re-running a "
        "full search."
    ),
    required_parameters=[
        ToolParameter(name="collection", type=str, description='Collection ID (e.g. "sentinel-2-l2a").'),
        ToolParameter(name="item_id", type=str, description="STAC item ID returned by search_stac_items."),
    ],
    optional_parameters=[
        ToolParameter(
            name="asset_keys",
            type=list,
            description='Optional list of asset keys to return (e.g. ["B04","B08"]). All keys returned if omitted.',
            default=None,
        ),
    ],
    return_spec=[
        ReturnSpec(name="item_id", type=str, description="STAC item ID."),
        ReturnSpec(name="collection", type=str, description="Collection ID."),
        ReturnSpec(name="datetime", type=str, description="Item acquisition datetime (ISO8601)."),
        ReturnSpec(name="bbox", type=list, description="Item bbox in EPSG:4326."),
        ReturnSpec(name="assets", type=dict, description="Asset key → {href, type, title, roles}."),
    ],
    module="earthscience_tools.get_item_assets",
    state_transition=StateTransition(
        requires=frozenset({"earthscience.items_searched"}),
        produces=frozenset({"earthscience.assets_resolved"}),
    ),
    affordances=[
        "get signed download URLs for raster bands",
        "refresh STAC asset URLs",
        "look up a STAC item by id",
    ],
)

# ============================================================================
# Medium complexity: Vegetation monitoring (raster I/O)
# ============================================================================

compute_ndvi = ToolDefinition(
    name="compute_ndvi",
    description=(
        "Compute NDVI = (NIR − Red) / (NIR + Red) from two band hrefs (e.g. "
        "Sentinel-2 B04 + B08, or Landsat red + nir08), optionally subset "
        "to a bbox and downsampled to honour max_pixels. Writes the result "
        "as a Cloud-Optimized GeoTIFF and returns summary stats."
    ),
    required_parameters=[
        ToolParameter(name="red_href", type=str, description="URL or path to the red-band raster."),
        ToolParameter(name="nir_href", type=str, description="URL or path to the NIR-band raster."),
    ],
    optional_parameters=[
        ToolParameter(
            name="bbox",
            type=list,
            description="Subset bbox [w,s,e,n] in EPSG:4326. Defaults to full raster extent.",
            default=None,
        ),
        ToolParameter(
            name="max_pixels",
            type=int,
            description="Approximate cap on output pixels (default 1,000,000; 0 = full res).",
            default=1000000,
        ),
        ToolParameter(
            name="output_path",
            type=str,
            description="Where to write the NDVI GeoTIFF. Auto-generated under /tmp if omitted.",
            default=None,
        ),
    ],
    return_spec=[
        ReturnSpec(name="output_path", type=str, description="Path to the written NDVI GeoTIFF."),
        ReturnSpec(name="shape", type=list, description="[height, width] of the NDVI raster."),
        ReturnSpec(name="crs", type=str, description="CRS of the NDVI raster."),
        ReturnSpec(name="bounds", type=list, description="Geographic bounds of the NDVI raster."),
        ReturnSpec(name="ndvi_min", type=float, description="Minimum NDVI over valid pixels."),
        ReturnSpec(name="ndvi_max", type=float, description="Maximum NDVI over valid pixels."),
        ReturnSpec(name="ndvi_mean", type=float, description="Mean NDVI over valid pixels."),
        ReturnSpec(name="ndvi_std", type=float, description="Std-dev NDVI over valid pixels."),
        ReturnSpec(name="valid_pixels", type=int, description="Count of finite NDVI pixels."),
        ReturnSpec(name="total_pixels", type=int, description="Total pixels in the output raster."),
    ],
    module="earthscience_tools.compute_ndvi",
    state_transition=StateTransition(
        requires=frozenset({"earthscience.items_searched"}),
        produces=frozenset({"earthscience.ndvi_computed"}),
    ),
    affordances=[
        "compute NDVI",
        "calculate a vegetation index",
        "produce a vegetation raster",
        "compute NDVI from Sentinel-2 or Landsat",
    ],
)

clip_to_geometry = ToolDefinition(
    name="clip_to_geometry",
    description=(
        "Clip a raster to a GeoJSON Geometry, Feature, or FeatureCollection. "
        "Geometries are reprojected to the raster's CRS before masking. "
        "Returns a new GeoTIFF cropped to the union bbox."
    ),
    required_parameters=[
        ToolParameter(name="raster_path", type=str, description="Path or URL of the raster to clip."),
        ToolParameter(
            name="geometry_geojson",
            type=dict,
            description="GeoJSON Geometry/Feature/FeatureCollection in EPSG:4326.",
        ),
    ],
    optional_parameters=[
        ToolParameter(
            name="output_path",
            type=str,
            description="Destination GeoTIFF. Auto-generated under /tmp if omitted.",
            default=None,
        ),
        ToolParameter(
            name="all_touched",
            type=bool,
            description="Include any pixel touched by the geometry (default False).",
            default=False,
        ),
    ],
    return_spec=[
        ReturnSpec(name="output_path", type=str, description="Path to the clipped raster."),
        ReturnSpec(name="shape", type=list, description="[height, width] of the clipped raster."),
        ReturnSpec(name="crs", type=str, description="CRS of the clipped raster."),
        ReturnSpec(name="bounds", type=list, description="Geographic bounds of the clipped raster."),
        ReturnSpec(name="input_bbox_4326", type=list, description="Union bbox of input geometries (EPSG:4326)."),
        ReturnSpec(name="num_features", type=int, description="Number of input geometries."),
    ],
    module="earthscience_tools.clip_to_geometry",
    state_transition=StateTransition(
        requires=frozenset({"earthscience.ndvi_computed"}),
        produces=frozenset({"earthscience.raster_clipped"}),
    ),
    affordances=[
        "clip a raster to a polygon",
        "subset imagery to an AOI",
        "mask raster pixels outside a region",
    ],
)

zonal_statistics = ToolDefinition(
    name="zonal_statistics",
    description=(
        "Compute per-polygon raster summary statistics. For each input "
        "polygon, masks the raster to that polygon (reprojecting the "
        "geometry to the raster's CRS) and reports the requested stats "
        "over valid pixels. Useful for 'mean NDVI per county' tasks."
    ),
    required_parameters=[
        ToolParameter(name="raster_path", type=str, description="Path or URL of the raster."),
        ToolParameter(
            name="polygons_geojson",
            type=dict,
            description="GeoJSON Feature/FeatureCollection of polygons in EPSG:4326.",
        ),
    ],
    optional_parameters=[
        ToolParameter(
            name="stats",
            type=list,
            description='Statistics to compute. Default ["mean","min","max","std","count"]. '
            'Valid: mean, min, max, std, median, count, sum.',
            default=None,
        ),
        ToolParameter(
            name="id_field",
            type=str,
            description="Feature property to copy as the row 'id' (e.g. 'GEOID' or 'NAME').",
            default=None,
        ),
        ToolParameter(name="band", type=int, description="1-based band index (default 1).", default=1),
        ToolParameter(
            name="all_touched",
            type=bool,
            description="Include any pixel touched by the polygon (default False).",
            default=False,
        ),
    ],
    return_spec=[
        ReturnSpec(name="num_features", type=int, description="Number of polygons evaluated."),
        ReturnSpec(name="stats_requested", type=list, description="Statistics computed for each polygon."),
        ReturnSpec(
            name="results",
            type=list,
            description="Per-polygon dict with id, feature_index, and one entry per requested stat.",
        ),
    ],
    module="earthscience_tools.zonal_statistics",
    state_transition=StateTransition(
        requires=frozenset({"earthscience.ndvi_computed"}),
        produces=frozenset({"earthscience.zonal_stats_computed"}),
    ),
    affordances=[
        "compute zonal statistics",
        "summarise raster values per polygon",
        "compute mean NDVI per region",
        "compute mean NDVI per county or tract",
    ],
)
