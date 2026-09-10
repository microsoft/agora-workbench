"""Structural extension protocols for data-lake catalogs and resolvers."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import (
    ArtifactReference,
    CatalogArtifact,
    ListRequest,
    Page,
    RequestContext,
    ResolvedArtifact,
    SearchRequest,
    SourceCapabilities,
)


@runtime_checkable
class ArtifactResolver(Protocol):
    """Resolve an opaque artifact ID to a fetchable storage location.

    Implementations may define ``async def aclose(self) -> None`` for cleanup,
    but it is intentionally not required by the protocol.
    """

    async def resolve(self, artifact_id: str) -> str:
        """Resolve an artifact ID to a qualified name or URL."""
        ...

    @property
    def unavailable_reason(self) -> str | None:
        """Return an operator-facing unavailability reason, or ``None`` when ready."""
        ...


@runtime_checkable
class CatalogProvider(Protocol):
    """Backend-neutral asynchronous catalog interface.

    Runtime checks only verify that named members exist. They do not validate
    signatures, async behavior, return types, or capability truthfulness.
    ``capabilities`` values are authoritative for provider support; callers
    must not infer support from method presence. Authorization policy is
    composed outside the provider protocol.
    """

    async def capabilities(self) -> tuple[SourceCapabilities, ...]:
        """Return authoritative read support for each source exposed by the provider."""
        ...

    async def search(self, request: SearchRequest, context: RequestContext) -> Page[CatalogArtifact]:
        """Search artifacts using provider-defined ranking and opaque cursors."""
        ...

    async def list(self, request: ListRequest, context: RequestContext) -> Page[CatalogArtifact]:
        """List artifacts using provider-defined filters and opaque cursors."""
        ...

    async def get(self, reference: ArtifactReference, context: RequestContext) -> CatalogArtifact:
        """Get one artifact by logical reference."""
        ...

    async def resolve(self, reference: ArtifactReference, context: RequestContext) -> ResolvedArtifact:
        """Resolve a logical reference to a physical storage locator."""
        ...
