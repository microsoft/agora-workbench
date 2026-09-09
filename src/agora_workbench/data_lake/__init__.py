"""Public contracts and compatibility exports for data-lake access."""

from agora_workbench.code_execution.data_access.fetchers import AssetFetcher, BlobFetcher, LocalFileFetcher
from agora_workbench.code_execution.data_access.manager import DataLakeDataManager
from agora_workbench.code_execution.data_access.publishers import (
    AssetPublisher,
    BlobPublisher,
    GuiPublisher,
    LocalFilePublisher,
    ServerPublisher,
)
from .catalog import CatalogConfig, CatalogDB, CatalogIndexer, SearchConfig, SourceConfig
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
from .resolvers import SearchIndexArtifactResolver


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
