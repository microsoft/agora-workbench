"""
Data access module for DataLake catalog integration.

Provides infrastructure for fetching and caching data assets from various
sources referenced by DataLake qualified names.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .artifact_resolvers import ArtifactResolver, SearchIndexArtifactResolver
    from .credentials import MsalCacheCredential, create_storage_credential
    from .publishers import (
        AssetPublisher,
        BlobPublisher,
        GuiPublisher,
        LocalFilePublisher,
        ServerPublisher,
        parse_destination_tag,
    )
    from .resolution import AssetResolutionMiddleware, looks_like_qualified_name, should_resolve_as_asset

_LAZY_EXPORTS = {
    "ArtifactResolver": ("agora_workbench.code_execution.data_access.artifact_resolvers", "ArtifactResolver"),
    "SearchIndexArtifactResolver": (
        "agora_workbench.code_execution.data_access.artifact_resolvers",
        "SearchIndexArtifactResolver",
    ),
    "MsalCacheCredential": ("agora_workbench.code_execution.data_access.credentials", "MsalCacheCredential"),
    "create_storage_credential": (
        "agora_workbench.code_execution.data_access.credentials",
        "create_storage_credential",
    ),
    "AssetPublisher": ("agora_workbench.code_execution.data_access.publishers", "AssetPublisher"),
    "BlobPublisher": ("agora_workbench.code_execution.data_access.publishers", "BlobPublisher"),
    "GuiPublisher": ("agora_workbench.code_execution.data_access.publishers", "GuiPublisher"),
    "LocalFilePublisher": ("agora_workbench.code_execution.data_access.publishers", "LocalFilePublisher"),
    "ServerPublisher": ("agora_workbench.code_execution.data_access.publishers", "ServerPublisher"),
    "parse_destination_tag": ("agora_workbench.code_execution.data_access.publishers", "parse_destination_tag"),
    "AssetResolutionMiddleware": (
        "agora_workbench.code_execution.data_access.resolution",
        "AssetResolutionMiddleware",
    ),
    "looks_like_qualified_name": (
        "agora_workbench.code_execution.data_access.resolution",
        "looks_like_qualified_name",
    ),
    "should_resolve_as_asset": (
        "agora_workbench.code_execution.data_access.resolution",
        "should_resolve_as_asset",
    ),
}


def __getattr__(name: str) -> object:
    try:
        module_name, attribute_name = _LAZY_EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | _LAZY_EXPORTS.keys())


__all__ = [
    "ArtifactResolver",
    "SearchIndexArtifactResolver",
    "MsalCacheCredential",
    "create_storage_credential",
    "AssetPublisher",
    "BlobPublisher",
    "GuiPublisher",
    "LocalFilePublisher",
    "ServerPublisher",
    "parse_destination_tag",
    "AssetResolutionMiddleware",
    "looks_like_qualified_name",
    "should_resolve_as_asset",
]
