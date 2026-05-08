"""Compute zonal statistics: per-polygon raster summaries."""

from __future__ import annotations

from typing import Optional, Union

_DEFAULT_STATS = ["mean", "min", "max", "std", "count"]
_VALID_STATS = {"mean", "min", "max", "std", "median", "count", "sum"}


def zonal_statistics(
    raster_path: str,
    polygons_geojson: Union[dict, list],
    stats: Optional[list] = None,
    id_field: Optional[str] = None,
    band: int = 1,
    all_touched: bool = False,
) -> dict:
    """Compute per-polygon summary statistics for a raster band.

    For each polygon in *polygons_geojson*, masks the raster to that polygon
    (geometry first reprojected to the raster's CRS) and computes the
    requested statistics over valid (non-nodata, finite) pixels.

    Args:
        raster_path: Path or URL of the raster.
        polygons_geojson: GeoJSON Feature / FeatureCollection / list of
            Features or Geometries in EPSG:4326.
        stats: Statistic names to compute. Default
            ``["mean","min","max","std","count"]``. Valid values:
            ``mean``, ``min``, ``max``, ``std``, ``median``, ``count``,
            ``sum``.
        id_field: Name of a feature property to copy into each result row
            as ``id`` (e.g. a county FIPS or tract GEOID). Falls back to
            the feature's ``id`` field, then ``feature_index``.
        band: 1-based band index to read (default 1).
        all_touched: If True, include any pixel touched by the polygon.

    Returns:
        Dictionary with ``num_features``, ``stats_requested``, and
        ``results`` (list of per-polygon dicts containing ``id``,
        ``feature_index``, and one key per requested statistic; ``None``
        when the polygon contains no valid pixels).
    """
    import numpy as np
    import rasterio
    from rasterio.mask import mask as rio_mask
    from rasterio.warp import transform_geom
    from shapely.geometry import shape

    requested = list(stats) if stats else list(_DEFAULT_STATS)
    invalid = [s for s in requested if s not in _VALID_STATS]
    if invalid:
        raise ValueError(
            f"Unsupported statistic(s) {invalid!r}. Valid: {sorted(_VALID_STATS)!r}"
        )

    features = _extract_features(polygons_geojson)
    if not features:
        raise ValueError("No features found in polygons_geojson.")

    results: list[dict] = []
    with rasterio.open(raster_path) as src:
        for idx, feature in enumerate(features):
            geom_4326 = feature["geometry"]
            geom = transform_geom("EPSG:4326", src.crs, geom_4326)
            try:
                clipped, _ = rio_mask(
                    src,
                    [geom],
                    crop=True,
                    all_touched=all_touched,
                    filled=False,  # masked array — nodata excluded
                    indexes=band,
                )
            except ValueError:
                # Polygon does not intersect raster.
                clipped = np.ma.masked_all((1, 1), dtype="float32")

            data = clipped if isinstance(clipped, np.ma.MaskedArray) else np.ma.asarray(clipped)
            data = data.astype("float64")
            # Also drop non-finite pixels (e.g. NaN nodata that masked_array missed).
            data.mask = data.mask | ~np.isfinite(np.asarray(data))
            valid = data.compressed()

            row: dict = {
                "feature_index": idx,
                "id": _resolve_id(feature, idx, id_field),
            }
            if valid.size == 0:
                for s in requested:
                    row[s] = None
            else:
                computed = {
                    "mean": float(valid.mean()),
                    "min": float(valid.min()),
                    "max": float(valid.max()),
                    "std": float(valid.std()),
                    "median": float(np.median(valid)),
                    "count": int(valid.size),
                    "sum": float(valid.sum()),
                }
                for s in requested:
                    row[s] = computed[s]
            results.append(row)

    return {
        "num_features": len(features),
        "stats_requested": requested,
        "results": results,
    }


def _extract_features(geojson):
    """Normalise input into a list of feature-like dicts with geometry."""
    items: list[dict] = []
    if isinstance(geojson, list):
        items.extend(g for g in geojson if isinstance(g, dict))
    elif isinstance(geojson, dict):
        gtype = geojson.get("type")
        if gtype == "FeatureCollection":
            items.extend(geojson.get("features", []))
        elif gtype == "Feature":
            items.append(geojson)
        elif gtype:
            items.append({"type": "Feature", "geometry": geojson, "properties": {}})

    features: list[dict] = []
    for entry in items:
        if entry.get("type") == "Feature":
            if entry.get("geometry"):
                features.append(entry)
        elif entry.get("type"):  # bare geometry inside list
            features.append({"type": "Feature", "geometry": entry, "properties": {}})
    return features


def _resolve_id(feature: dict, idx: int, id_field: str | None):
    if id_field:
        props = feature.get("properties") or {}
        if id_field in props:
            return props[id_field]
    if "id" in feature:
        return feature["id"]
    return idx
