"""
Data access module for DataLake catalog integration.

Provides infrastructure for fetching and caching data assets from various
sources referenced by DataLake qualified names.
"""

from .resolution import (
    AssetResolutionMiddleware,
    looks_like_qualified_name,
    should_resolve_as_asset,
)

__all__ = [
    "AssetResolutionMiddleware",
    "looks_like_qualified_name",
    "should_resolve_as_asset",
]
