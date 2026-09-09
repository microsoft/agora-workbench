"""Public contracts and compatibility exports for data-lake access."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

from .errors import (
    ArtifactNotFoundError,
    BackendUnavailableError,
    DataLakeError,
    DataLakeErrorCode,
    InvalidRequestError,
    PermissionDeniedError,
    UnsupportedOperationError,
)
from .models import (
    MAX_PAGE_LIMIT,
    READ_OPERATIONS,
    ArtifactPresentation,
    ArtifactReference,
    CatalogArtifact,
    CatalogOperation,
    DownloadInfo,
    ListRequest,
    Page,
    PageRequest,
    RequestContext,
    ResolvedArtifact,
    ResourceLease,
    ResourceOwnership,
    SearchRequest,
    SourceCapabilities,
    StorageLocator,
)
from .protocols import ArtifactResolver, CatalogProvider

if TYPE_CHECKING:
    from agora_workbench.code_execution.data_access.catalog import (
        CatalogConfig,
        CatalogDB,
        CatalogIndexer,
        SearchConfig,
        SourceConfig,
    )
    from agora_workbench.code_execution.data_access.fetchers import AssetFetcher, BlobFetcher, LocalFileFetcher
    from agora_workbench.code_execution.data_access.manager import DataLakeDataManager
    from agora_workbench.code_execution.data_access.publishers import (
        AssetPublisher,
        BlobPublisher,
        GuiPublisher,
        LocalFilePublisher,
        ServerPublisher,
    )
    from agora_workbench.code_execution.data_access.artifact_resolvers import SearchIndexArtifactResolver

_LAZY_EXPORTS = {
    "AssetFetcher": ("agora_workbench.code_execution.data_access.fetchers", "AssetFetcher"),
    "AssetPublisher": ("agora_workbench.code_execution.data_access.publishers", "AssetPublisher"),
    "BlobFetcher": ("agora_workbench.code_execution.data_access.fetchers", "BlobFetcher"),
    "BlobPublisher": ("agora_workbench.code_execution.data_access.publishers", "BlobPublisher"),
    "CatalogConfig": ("agora_workbench.code_execution.data_access.catalog", "CatalogConfig"),
    "CatalogDB": ("agora_workbench.code_execution.data_access.catalog", "CatalogDB"),
    "CatalogIndexer": ("agora_workbench.code_execution.data_access.catalog", "CatalogIndexer"),
    "DataLakeDataManager": ("agora_workbench.code_execution.data_access.manager", "DataLakeDataManager"),
    "GuiPublisher": ("agora_workbench.code_execution.data_access.publishers", "GuiPublisher"),
    "LocalFileFetcher": ("agora_workbench.code_execution.data_access.fetchers", "LocalFileFetcher"),
    "LocalFilePublisher": ("agora_workbench.code_execution.data_access.publishers", "LocalFilePublisher"),
    "SearchConfig": ("agora_workbench.code_execution.data_access.catalog", "SearchConfig"),
    "SearchIndexArtifactResolver": (
        "agora_workbench.code_execution.data_access.artifact_resolvers",
        "SearchIndexArtifactResolver",
    ),
    "ServerPublisher": ("agora_workbench.code_execution.data_access.publishers", "ServerPublisher"),
    "SourceConfig": ("agora_workbench.code_execution.data_access.catalog", "SourceConfig"),
}
_LAZY_SUBMODULES = {"catalog", "errors", "models", "protocols", "resolvers"}


def __getattr__(name: str) -> object:
    if name in _LAZY_SUBMODULES:
        value = import_module(f"{__name__}.{name}")
        globals()[name] = value
        return value
    try:
        module_name, attribute_name = _LAZY_EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | _LAZY_EXPORTS.keys() | _LAZY_SUBMODULES)


__all__ = [
    "ArtifactNotFoundError",
    "ArtifactPresentation",
    "ArtifactReference",
    "ArtifactResolver",
    "AssetFetcher",
    "AssetPublisher",
    "BackendUnavailableError",
    "BlobFetcher",
    "BlobPublisher",
    "CatalogArtifact",
    "CatalogConfig",
    "CatalogDB",
    "CatalogIndexer",
    "CatalogOperation",
    "CatalogProvider",
    "DataLakeDataManager",
    "DataLakeError",
    "DataLakeErrorCode",
    "DownloadInfo",
    "GuiPublisher",
    "InvalidRequestError",
    "ListRequest",
    "LocalFileFetcher",
    "LocalFilePublisher",
    "MAX_PAGE_LIMIT",
    "Page",
    "PageRequest",
    "PermissionDeniedError",
    "READ_OPERATIONS",
    "RequestContext",
    "ResolvedArtifact",
    "ResourceLease",
    "ResourceOwnership",
    "SearchConfig",
    "SearchIndexArtifactResolver",
    "SearchRequest",
    "ServerPublisher",
    "SourceCapabilities",
    "SourceConfig",
    "StorageLocator",
    "UnsupportedOperationError",
]
