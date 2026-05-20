"""Resolve and sign assets for a single STAC item."""

from __future__ import annotations

from typing import Optional

_PC_STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"


def get_item_assets(
    collection: str,
    item_id: str,
    asset_keys: Optional[list] = None,
) -> dict:
    """Fetch a single STAC item and return its signed asset URLs.

    Useful when you already know the collection + item ID (from a previous
    ``search_stac_items`` call) and want fresh signed URLs without re-running
    a search. Planetary Computer signed URLs are short-lived (~1 hour).

    Args:
        collection: Collection ID (e.g. ``"sentinel-2-l2a"``).
        item_id: STAC item ID returned by ``search_stac_items``.
        asset_keys: Optional list of asset keys to include
            (e.g. ``["B04", "B08"]``). If omitted, all assets are returned.

    Returns:
        Dictionary with ``item_id``, ``collection``, ``datetime``, ``bbox``,
        and ``assets`` (mapping asset key → ``{"href", "type", "title",
        "roles"}``).
    """
    import planetary_computer
    import pystac_client

    catalog = pystac_client.Client.open(
        _PC_STAC_URL,
        modifier=planetary_computer.sign_inplace,
    )

    collection_obj = catalog.get_collection(collection)
    item = collection_obj.get_item(item_id)
    if item is None:
        raise ValueError(
            f"STAC item {item_id!r} not found in collection {collection!r}"
        )

    requested_keys = set(asset_keys) if asset_keys else None
    assets: dict[str, dict] = {}
    for key, asset in item.assets.items():
        if requested_keys is not None and key not in requested_keys:
            continue
        assets[key] = {
            "href": asset.href,
            "type": asset.media_type,
            "title": asset.title,
            "roles": list(asset.roles or []),
        }

    if requested_keys is not None:
        missing = sorted(requested_keys - assets.keys())
        if missing:
            available = sorted(item.assets.keys())
            raise ValueError(
                f"Asset key(s) {missing!r} not found on item {item_id!r}. "
                f"Available: {available!r}"
            )

    return {
        "item_id": item.id,
        "collection": collection,
        "datetime": item.datetime.isoformat() if item.datetime else None,
        "bbox": list(item.bbox) if item.bbox else None,
        "assets": assets,
    }
