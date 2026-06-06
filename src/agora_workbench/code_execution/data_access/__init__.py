"""
Data access module for DataLake catalog integration.

Provides infrastructure for fetching and caching data assets from various
sources referenced by DataLake qualified names.
"""

from .credentials import (
    MsalCacheCredential,
    create_storage_credential,
)
from .publishers import (
    AssetPublisher,
    BlobPublisher,
    GuiPublisher,
    LocalFilePublisher,
    parse_destination_tag,
)
from .resolution import (
    AssetResolutionMiddleware,
    looks_like_qualified_name,
    should_resolve_as_asset,
)

__all__ = [
    "MsalCacheCredential",
    "create_storage_credential",
    "AssetPublisher",
    "BlobPublisher",
    "GuiPublisher",
    "LocalFilePublisher",
    "parse_destination_tag",
    "AssetResolutionMiddleware",
    "looks_like_qualified_name",
    "should_resolve_as_asset",
]
