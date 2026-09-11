"""Structural extension protocols for data-lake catalogs and resolvers."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from .models import (
    ArtifactReference,
    CatalogAuthorizationRequest,
    CatalogArtifact,
    CatalogPolicyMode,
    ListRequest,
    Page,
    RequestContext,
    ResolvedArtifact,
    SearchRequest,
    SourceCapabilities,
)
from .transfer import TransferOptions, TransferResult


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
        """Get one artifact, honoring or explicitly rejecting a pinned revision."""
        ...

    async def resolve(self, reference: ArtifactReference, context: RequestContext) -> ResolvedArtifact:
        """Resolve a logical reference, honoring or explicitly rejecting a pinned revision."""
        ...


@runtime_checkable
class CatalogAuthorizer(Protocol):
    """Application-supplied caller policy, composed outside a catalog provider.

    A request without ``reference`` is source-scoped. In per-artifact mode,
    source-scoped approval advertises an operation as potentially available;
    the policy enforcer must additionally authorize each artifact before it can
    affect ranking, pagination, aggregation, lookup results, or errors.
    """

    async def authorize(self, request: CatalogAuthorizationRequest, context: RequestContext) -> bool:
        """Return whether the caller may perform the requested operation."""
        ...


@runtime_checkable
class CatalogPolicyEnforcer(Protocol):
    """Backend-specific, trusted enforcement for per-artifact policy.

    Implementations must apply policy before ranking, pagination, aggregation,
    alias resolution, and not-found decisions. Filtering one provider page
    after retrieval does not satisfy this contract.
    """

    async def search(
        self,
        provider: CatalogProvider,
        request: SearchRequest,
        context: RequestContext,
        authorizer: CatalogAuthorizer,
    ) -> Page[CatalogArtifact]:
        """Search only artifacts authorized for the current caller."""
        ...

    async def list(
        self,
        provider: CatalogProvider,
        request: ListRequest,
        context: RequestContext,
        authorizer: CatalogAuthorizer,
    ) -> Page[CatalogArtifact]:
        """List only artifacts authorized for the current caller."""
        ...

    async def get(
        self,
        provider: CatalogProvider,
        reference: ArtifactReference,
        context: RequestContext,
        authorizer: CatalogAuthorizer,
    ) -> CatalogArtifact:
        """Authorize lookup semantics before returning the exact requested logical reference."""
        ...

    async def resolve(
        self,
        provider: CatalogProvider,
        reference: ArtifactReference,
        context: RequestContext,
        authorizer: CatalogAuthorizer,
    ) -> ResolvedArtifact:
        """Resolve only an authorized canonical artifact."""
        ...


@runtime_checkable
class PolicyEnforcedCatalog(Protocol):
    """Caller-aware catalog surface produced by policy composition."""

    @property
    def policy_mode(self) -> CatalogPolicyMode:
        """Return the configured enforcement granularity."""
        ...

    async def capabilities(self, context: RequestContext) -> tuple[SourceCapabilities, ...]:
        """Return provider support intersected with current caller policy."""
        ...

    async def search(self, request: SearchRequest, context: RequestContext) -> Page[CatalogArtifact]:
        """Search within the current caller's effective authorization scope."""
        ...

    async def list(self, request: ListRequest, context: RequestContext) -> Page[CatalogArtifact]:
        """List within the current caller's effective authorization scope."""
        ...

    async def get(self, reference: ArtifactReference, context: RequestContext) -> CatalogArtifact:
        """Get an artifact without disclosing unauthorized existence."""
        ...

    async def resolve(self, reference: ArtifactReference, context: RequestContext) -> ResolvedArtifact:
        """Resolve an artifact without disclosing unauthorized existence."""
        ...


@runtime_checkable
class StreamingArtifactFetcher(Protocol):
    """Fetcher capability that commits a bounded stream to a local file.

    This is deliberately separate from full-memory ``fetch()`` conveniences.
    Implementations leave an existing destination unchanged on failure.
    """

    async def fetch_to_file(
        self,
        qualified_name: str,
        dest_path: Path,
        *,
        options: TransferOptions | None = None,
        context: RequestContext | None = None,
    ) -> int:
        """Stream an artifact to a file and return the committed byte count."""
        ...


@runtime_checkable
class StreamingArtifactPublisher(Protocol):
    """Publisher capability that uploads a local file with bounded memory."""

    async def publish(
        self,
        local_path: Path,
        name: str,
        session_id: str,
        *,
        options: TransferOptions | None = None,
        context: RequestContext | None = None,
    ) -> str:
        """Publish a file after enforcing the supplied transfer guarantees."""
        ...


@runtime_checkable
class DetailedStreamingArtifactFetcher(Protocol):
    """Optional detailed-result seam for managed-read and audit integrations."""

    async def fetch_to_file_result(
        self,
        qualified_name: str,
        dest_path: Path,
        *,
        options: TransferOptions | None = None,
        context: RequestContext | None = None,
    ) -> TransferResult:
        """Stream an artifact and return integrity and caller diagnostics."""
        ...


@runtime_checkable
class DetailedStreamingArtifactPublisher(Protocol):
    """Optional detailed-result seam for managed-write integrations."""

    async def publish_with_result(
        self,
        local_path: Path,
        name: str,
        session_id: str,
        *,
        options: TransferOptions | None = None,
        context: RequestContext | None = None,
    ) -> tuple[str, TransferResult]:
        """Publish a file and return its locator plus transfer result."""
        ...
