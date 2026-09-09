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


@runtime_checkable
class ArtifactResolver(Protocol):
    """Resolve an opaque artifact ID to a URL that an ``AssetFetcher`` can retrieve.

    ``DataLakeDataManager`` calls :meth:`resolve` on each manager cache miss;
    implementations are responsible for any backend-result caching. A resolver
    may additionally define ``async def aclose(self) -> None``. The manager
    calls that optional method during cleanup, but it is intentionally not a
    protocol member so runtime structural checks do not require it.

    A supplied resolver owns clients it creates and may close those clients.
    Credentials and other resources borrowed from its caller remain caller-owned
    and must not be closed by the resolver.

    Runtime checks only verify member presence, not signatures, async behavior,
    property types, or return values.
    """

    async def resolve(self, artifact_id: str) -> str:
        """Resolve an opaque artifact ID to a fetchable qualified name or URL.

        Raises:
            ValueError: If the artifact is unknown, the resolved location is
                invalid, or resolution is unavailable.
        """
        ...

    @property
    def unavailable_reason(self) -> str | None:
        """Return an operator-facing unavailability reason, or ``None`` when ready.

        The reason is surfaced in agent guidance and should explain required
        operator configuration without leaking backend internals.
        """
        ...
