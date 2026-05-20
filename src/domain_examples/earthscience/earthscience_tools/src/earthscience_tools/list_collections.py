"""List Microsoft Planetary Computer STAC collections."""

from __future__ import annotations

from typing import Optional

_PC_STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"


def list_collections(search: Optional[str] = None, max_results: int = 50) -> dict:
    """List STAC collections available on Microsoft Planetary Computer.

    Args:
        search: Optional case-insensitive substring filter applied to each
            collection's ``id``, ``title``, and ``description``.
        max_results: Cap on returned collections (default 50).

    Returns:
        Dictionary with ``num_total``, ``num_returned``, and ``collections``
        (list of ``{"id", "title", "description", "spatial_extent",
        "temporal_extent", "license", "keywords"}``).
    """
    import pystac_client

    catalog = pystac_client.Client.open(_PC_STAC_URL)
    all_collections = list(catalog.get_collections())

    needle = (search or "").strip().lower()

    def _matches(c) -> bool:
        if not needle:
            return True
        haystack = " ".join(
            [
                c.id or "",
                c.title or "",
                c.description or "",
            ]
        ).lower()
        return needle in haystack

    matched = [c for c in all_collections if _matches(c)]
    matched.sort(key=lambda c: c.id)
    truncated = matched[:max_results]

    def _spatial_extent(c) -> list[float] | None:
        try:
            bbox = c.extent.spatial.bboxes[0]
            return [float(x) for x in bbox]
        except (AttributeError, IndexError, TypeError):
            return None

    def _temporal_extent(c) -> list[str | None] | None:
        try:
            interval = c.extent.temporal.intervals[0]
            return [
                interval[0].isoformat() if interval[0] is not None else None,
                interval[1].isoformat() if interval[1] is not None else None,
            ]
        except (AttributeError, IndexError, TypeError):
            return None

    return {
        "num_total": len(matched),
        "num_returned": len(truncated),
        "collections": [
            {
                "id": c.id,
                "title": c.title,
                "description": (c.description or "")[:500],
                "spatial_extent": _spatial_extent(c),
                "temporal_extent": _temporal_extent(c),
                "license": c.license,
                "keywords": list(c.keywords or []),
            }
            for c in truncated
        ],
    }
