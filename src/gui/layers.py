"""Map layer and raster tile routes."""

import json
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pathlib import Path

from titiler.core.factory import TilerFactory

from .map_state import MAPS_DIR, read_map_state

LOGGER = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# GeoJSON layer serving
# ---------------------------------------------------------------------------


@router.get("/api/map-state")
async def get_map_state():
    state = read_map_state()
    if state is None:
        return JSONResponse(content={"view": None, "layers": []})
    return JSONResponse(content=state)


@router.get("/api/layers/{filename}")
async def get_layer(filename: str):
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    if not filename.endswith(".geojson"):
        raise HTTPException(status_code=400, detail="Only .geojson files allowed")

    path = MAPS_DIR / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Layer not found")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        LOGGER.warning("Failed to read layer file %s: %s", filename, e)
        raise HTTPException(status_code=500, detail="Failed to read layer file")

    if isinstance(data, dict) and data.get("type") == "FeatureCollection":
        features = data.get("features", [])
        if isinstance(features, list):
            null_geom = sum(1 for f in features if isinstance(f, dict) and not f.get("geometry"))
            if null_geom:
                LOGGER.warning("[%s] %d feature(s) with null geometry", filename, null_geom)

    return JSONResponse(content=data)


# ---------------------------------------------------------------------------
# Titiler raster tile server
# ---------------------------------------------------------------------------

tiler = TilerFactory(router_prefix="/api/cog")
router.include_router(tiler.router, prefix="/api/cog")

# Transparent 256x256 PNG for out-of-bounds tile requests
TRANSPARENT_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x01\x00\x00\x00\x01\x00"
    b"\x08\x06\x00\x00\x00\\\xbc\xa2b\x00\x00\x00\x1dIDATx\x9c\xed\xc1"
    b"\x01\r\x00\x00\x00\xc2\xa0\xf5Om\x0e7\xa0\x00\x00\x00\x00\x00\x00"
    b"\x00\x00\xbe\r!\x00\x00\x01\x9a`\xe1\xd5\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _resolve_tif_path(layer_id: str) -> Path:
    """Resolve a layer ID to the GeoTIFF path on disk."""
    if "/" in layer_id or "\\" in layer_id or ".." in layer_id:
        raise HTTPException(status_code=400, detail="Invalid layer name")

    state = read_map_state()
    tif_file = f"{layer_id}.tif"
    if state and state.get("layers"):
        for layer_entry in state["layers"]:
            if layer_entry.get("id") == layer_id:
                tif_file = layer_entry.get("tif_file", tif_file)
                break

    path = (MAPS_DIR / tif_file).resolve()
    if not path.is_relative_to(MAPS_DIR.resolve()):
        raise HTTPException(status_code=400, detail="Invalid raster filename")

    path = MAPS_DIR / tif_file
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Raster not found")
    return path


@router.get("/api/raster-tile-url/{layer}")
async def get_raster_tile_url(layer: str):
    """Return the titiler tile URL template for a raster layer."""
    tif_path = _resolve_tif_path(layer)
    file_url = tif_path.as_uri()

    state = read_map_state()
    params = [f"url={file_url}"]
    if state and state.get("layers"):
        for layer_entry in state["layers"]:
            if layer_entry.get("id") == layer:
                if layer_entry.get("colormap"):
                    params.append(f"colormap_name={layer_entry['colormap']}")
                if layer_entry.get("value_range"):
                    vr = layer_entry["value_range"]
                    params.append(f"rescale={vr[0]},{vr[1]}")
                break

    query = "&".join(params)
    tile_url = f"/api/cog/tiles/WebMercatorQuad/{{z}}/{{x}}/{{y}}?{query}"
    return JSONResponse(content={"tile_url": tile_url})
