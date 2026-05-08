"""Clip a raster to a GeoJSON geometry."""

from __future__ import annotations

import os
import tempfile
import uuid
from typing import Optional, Union


def clip_to_geometry(
    raster_path: str,
    geometry_geojson: Union[dict, list],
    output_path: Optional[str] = None,
    all_touched: bool = False,
) -> dict:
    """Clip a raster to a GeoJSON geometry / FeatureCollection.

    The geometry is reprojected to the raster's CRS before masking. Pixels
    outside the geometry are filled with the raster's nodata value (or NaN
    for floating-point rasters that lack one).

    Args:
        raster_path: Path or URL of the raster to clip.
        geometry_geojson: GeoJSON Geometry, Feature, FeatureCollection, or a
            list of Geometries / Features in EPSG:4326.
        output_path: Destination GeoTIFF. If ``None``, a unique file is
            created under ``/tmp``.
        all_touched: If True, include any pixel touched by the geometry
            (rasterio.mask default is False — only pixels whose centre is
            inside the geometry).

    Returns:
        Dictionary with ``output_path``, ``shape`` ``[height, width]``,
        ``crs``, ``bounds``, ``num_features``.
    """
    import rasterio
    from rasterio.mask import mask as rio_mask
    from rasterio.warp import transform_geom
    from shapely.geometry import shape

    geoms_4326 = _extract_geometries(geometry_geojson)
    if not geoms_4326:
        raise ValueError("No geometries found in geometry_geojson.")

    if output_path is None:
        output_path = os.path.join(
            tempfile.gettempdir(), f"clip_{uuid.uuid4().hex[:12]}.tif"
        )

    with rasterio.open(raster_path) as src:
        # Reproject geometries from EPSG:4326 to the raster CRS.
        reprojected = [
            transform_geom("EPSG:4326", src.crs, g.__geo_interface__)
            for g in geoms_4326
        ]
        clipped, clipped_transform = rio_mask(
            src,
            reprojected,
            crop=True,
            all_touched=all_touched,
            filled=True,
        )

        out_meta = src.meta.copy()
        out_meta.update(
            {
                "driver": "GTiff",
                "height": clipped.shape[1],
                "width": clipped.shape[2],
                "transform": clipped_transform,
                "compress": "deflate",
                "tiled": True,
            }
        )

        with rasterio.open(output_path, "w", **out_meta) as dst:
            dst.write(clipped)

        bounds = rasterio.windows.bounds(
            rasterio.windows.Window(0, 0, clipped.shape[2], clipped.shape[1]),
            clipped_transform,
        )

    # Cheap union bbox for caller convenience
    union = geoms_4326[0]
    for g in geoms_4326[1:]:
        union = union.union(g)

    return {
        "output_path": output_path,
        "shape": [int(clipped.shape[1]), int(clipped.shape[2])],
        "crs": str(out_meta["crs"]),
        "bounds": [float(b) for b in bounds],
        "input_bbox_4326": [float(x) for x in union.bounds],
        "num_features": len(geoms_4326),
    }


def _extract_geometries(geojson):
    """Normalise input into a list of shapely geometries (in EPSG:4326)."""
    from shapely.geometry import shape

    items: list[dict] = []
    if isinstance(geojson, list):
        for entry in geojson:
            if isinstance(entry, dict):
                items.append(entry)
    elif isinstance(geojson, dict):
        gtype = geojson.get("type")
        if gtype == "FeatureCollection":
            items.extend(geojson.get("features", []))
        elif gtype == "Feature":
            items.append(geojson)
        elif gtype:
            items.append(geojson)

    geoms = []
    for entry in items:
        if entry.get("type") == "Feature":
            geom = entry.get("geometry")
            if geom:
                geoms.append(shape(geom))
        elif entry.get("type"):
            geoms.append(shape(entry))
    return geoms
