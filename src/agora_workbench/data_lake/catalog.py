"""Public catalog contracts and lazy compatibility exports."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

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
    SearchRequest,
    SourceCapabilities,
    StorageLocator,
)
from .protocols import CatalogProvider

if TYPE_CHECKING:
    from agora_workbench.code_execution.data_access.catalog import (
        CatalogConfig,
        CatalogDB,
        CatalogIndexer,
        SearchConfig,
        SourceConfig,
    )

_LAZY_EXPORTS = {
    name: ("agora_workbench.code_execution.data_access.catalog", name)
    for name in ("CatalogConfig", "CatalogDB", "CatalogIndexer", "SearchConfig", "SourceConfig")
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
    "ArtifactPresentation",
    "ArtifactReference",
    "CatalogArtifact",
    "CatalogConfig",
    "CatalogDB",
    "CatalogIndexer",
    "CatalogOperation",
    "CatalogProvider",
    "DownloadInfo",
    "ListRequest",
    "MAX_PAGE_LIMIT",
    "Page",
    "PageRequest",
    "READ_OPERATIONS",
    "RequestContext",
    "ResolvedArtifact",
    "SearchConfig",
    "SearchRequest",
    "SourceCapabilities",
    "SourceConfig",
    "StorageLocator",
]
