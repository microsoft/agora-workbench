"""Backend-neutral records for the public data-lake API."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import ClassVar, Generic, TypeVar

from .errors import InvalidRequestError

MAX_PAGE_LIMIT = 1_000


def _immutable_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    """Copy caller-provided mappings behind a read-only view."""
    return MappingProxyType(dict(value))


class CatalogOperation(StrEnum):
    """Read operations a catalog provider may support."""

    SEARCH = "search"
    LIST = "list"
    GET = "get"
    RESOLVE = "resolve"


READ_OPERATIONS = frozenset(
    {
        CatalogOperation.SEARCH,
        CatalogOperation.LIST,
        CatalogOperation.GET,
        CatalogOperation.RESOLVE,
    }
)


@dataclass(frozen=True)
class RequestContext:
    """Mutable-caller metadata copied for propagation, not cache identity."""

    request_id: str | None = None
    caller_id: str | None = None
    attributes: Mapping[str, object] = field(default_factory=dict)
    __hash__: ClassVar[None] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "attributes", _immutable_mapping(self.attributes))


@dataclass(frozen=True)
class PageRequest:
    """Cursor-based pagination request, capped at :data:`MAX_PAGE_LIMIT`."""

    limit: int = 50
    cursor: str | None = None

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise InvalidRequestError("Page limit must be at least 1.", operation="pagination")
        if self.limit > MAX_PAGE_LIMIT:
            raise InvalidRequestError(
                f"Page limit must not exceed {MAX_PAGE_LIMIT}.",
                operation="pagination",
            )


T = TypeVar("T")


@dataclass(frozen=True)
class Page(Generic[T]):
    """One page of results and an opaque provider cursor."""

    items: tuple[T, ...]
    next_cursor: str | None = None


@dataclass(frozen=True)
class ArtifactReference:
    """Stable logical identity ``(source_id, artifact_id)``, independent of storage."""

    artifact_id: str
    source_id: str


@dataclass(frozen=True)
class StorageLocator:
    """Physical locator understood by a fetcher or storage provider."""

    uri: str


@dataclass(frozen=True)
class ArtifactPresentation:
    """Human-facing catalog metadata."""

    name: str
    description: str | None = None
    media_type: str | None = None
    size_bytes: int | None = None


@dataclass(frozen=True)
class DownloadInfo:
    """Optional presentation-layer download information."""

    url: str
    filename: str | None = None
    expires_at: datetime | None = None


@dataclass(frozen=True)
class CatalogArtifact:
    """Backend-neutral artifact returned by catalog operations."""

    reference: ArtifactReference
    presentation: ArtifactPresentation
    locator: StorageLocator | None = None
    download: DownloadInfo | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _immutable_mapping(self.metadata))


@dataclass(frozen=True)
class ResolvedArtifact:
    """A logical artifact reference resolved to a physical locator."""

    reference: ArtifactReference
    locator: StorageLocator


@dataclass(frozen=True)
class SearchRequest:
    """Catalog search request."""

    query: str
    source_ids: tuple[str, ...] = ()
    page: PageRequest = field(default_factory=PageRequest)
    filters: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_ids", tuple(self.source_ids))
        object.__setattr__(self, "filters", _immutable_mapping(self.filters))


@dataclass(frozen=True)
class ListRequest:
    """Catalog listing request."""

    source_ids: tuple[str, ...] = ()
    page: PageRequest = field(default_factory=PageRequest)
    filters: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_ids", tuple(self.source_ids))
        object.__setattr__(self, "filters", _immutable_mapping(self.filters))


@dataclass(frozen=True)
class SourceCapabilities:
    """Read operations authoritatively supported by one provider source."""

    source_id: str
    supported_operations: frozenset[CatalogOperation]

    def __post_init__(self) -> None:
        object.__setattr__(self, "supported_operations", frozenset(self.supported_operations))

    def supports(self, operation: CatalogOperation) -> bool:
        """Return whether the source reports support for *operation*."""
        return operation in self.supported_operations


class ResourceOwnership(StrEnum):
    """Whether the recipient owns cleanup of a supplied resource."""

    OWNED = "owned"
    BORROWED = "borrowed"


@dataclass(frozen=True)
class ResourceLease(Generic[T]):
    """A resource paired with its explicit cleanup ownership."""

    resource: T
    ownership: ResourceOwnership = ResourceOwnership.BORROWED

    @property
    def should_close(self) -> bool:
        """Whether the recipient is responsible for closing the resource."""
        return self.ownership is ResourceOwnership.OWNED
