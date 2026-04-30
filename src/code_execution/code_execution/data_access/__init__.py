"""
Data access module for DataLake catalog integration.

Provides infrastructure for fetching and caching data assets from various
sources referenced by DataLake qualified names.
"""

from .manager import DataLakeDataManager
from .resolution import (
    AssetResolutionMiddleware,
    _resolved_assets,
    looks_like_qualified_name,
    should_resolve_as_asset,
)

__all__ = [
    "AssetResolutionMiddleware",
    "DataLakeDataManager",
    "_resolved_assets",
    "looks_like_qualified_name",
    "should_resolve_as_asset",
]
