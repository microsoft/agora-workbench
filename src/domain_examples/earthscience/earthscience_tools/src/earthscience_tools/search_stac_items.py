"""Search STAC items on Microsoft Planetary Computer."""

from __future__ import annotations

from typing import Optional

_PC_STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"


def search_stac_items(
    collection: str,
    bbox: Optional[list] = None,
    datetime: Optional[str] = None,
    cloud_cover_lt: Optional[float] = None,
    query: Optional[dict] = None,
    max_items: int = 25,
) -> dict:
    """Search STAC items in a Planetary Computer collection.

    Args:
        collection: Collection ID (e.g. ``"sentinel-2-l2a"``,
            ``"landsat-c2-l2"``, ``"cop-dem-glo-30"``).
        bbox: Bounding box ``[west, south, east, north]`` in EPSG:4326.
        datetime: Date or date range in RFC 3339 / STAC format
            (e.g. ``"2024-06-01"``, ``"2024-06-01/2024-06-30"``).
        cloud_cover_lt: Filter to items with ``eo:cloud_cover`` strictly
            less than this percentage (only meaningful for optical
            collections).
        query: Additional STAC query-extension filters merged on top of
            ``cloud_cover_lt``. Example: ``{"platform": {"eq": "sentinel-2a"}}``.
        max_items: Cap on returned items (default 25, hard limit 200).

    Returns:
        Dictionary with ``num_items``, ``collection``, and ``items``.
        Each item carries ``id``, ``datetime``, ``bbox``, ``cloud_cover``
        (when present), ``platform`` (when present), and ``assets`` mapping
        asset key → signed download URL.
    """
    import planetary_computer
    import pystac_client

    catalog = pystac_client.Client.open(
        _PC_STAC_URL,
        modifier=planetary_computer.sign_inplace,
    )

    merged_query: dict = dict(query or {})
    if cloud_cover_lt is not None:
        merged_query["eo:cloud_cover"] = {"lt": float(cloud_cover_lt)}

    capped = max(1, min(int(max_items), 200))

    search = catalog.search(
        collections=[collection],
        bbox=bbox,
        datetime=datetime,
        query=merged_query or None,
        max_items=capped,
    )

    items = list(search.items())

    def _serialize(item) -> dict:
        props = item.properties or {}
        return {
            "id": item.id,
            "collection": item.collection_id,
            "datetime": item.datetime.isoformat() if item.datetime else None,
            "bbox": list(item.bbox) if item.bbox else None,
            "cloud_cover": props.get("eo:cloud_cover"),
            "platform": props.get("platform"),
            "instruments": props.get("instruments"),
            "assets": {key: asset.href for key, asset in item.assets.items()},
        }

    return {
        "num_items": len(items),
        "collection": collection,
        "items": [_serialize(item) for item in items],
    }
