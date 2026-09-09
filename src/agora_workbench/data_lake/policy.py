"""Caller-aware authorization composition for catalog providers."""

from __future__ import annotations

from dataclasses import replace
from typing import Never, TypeVar

from .errors import ArtifactNotFoundError, InvalidRequestError, PermissionDeniedError, UnsupportedOperationError
from .models import (
    ArtifactReference,
    CatalogAuthorizationRequest,
    CatalogArtifact,
    CatalogOperation,
    CatalogPolicyMode,
    ListRequest,
    Page,
    RequestContext,
    ResolvedArtifact,
    SearchRequest,
    SourceCapabilities,
)
from .protocols import CatalogAuthorizer, CatalogPolicyEnforcer, CatalogProvider

RequestT = TypeVar("RequestT", SearchRequest, ListRequest)


class DenyAllCatalogAuthorizer:
    """Fail-closed policy suitable as an explicit production baseline."""

    async def authorize(self, request: CatalogAuthorizationRequest, context: RequestContext) -> bool:
        return False


class DevelopmentAllowAllCatalogAuthorizer:
    """Explicit development-only policy that permits every catalog request."""

    async def authorize(self, request: CatalogAuthorizationRequest, context: RequestContext) -> bool:
        return True


class AuthorizedCatalogProvider:
    """Compose caller policy around an untrusted-to-authorize provider.

    Providers still enforce ordinary query constraints such as ``source_ids``;
    they never decide caller policy. Per-artifact mode additionally requires a
    backend-specific policy enforcer because generic post-filtering cannot
    preserve ranking, pagination, aggregation, or alias confidentiality.
    """

    def __init__(
        self,
        provider: CatalogProvider,
        authorizer: CatalogAuthorizer,
        *,
        mode: CatalogPolicyMode,
        per_artifact_enforcer: CatalogPolicyEnforcer | None = None,
    ) -> None:
        if mode is not CatalogPolicyMode.HOMOGENEOUS_SOURCE and mode is not CatalogPolicyMode.PER_ARTIFACT:
            raise ValueError(f"Unknown catalog policy mode: {mode!r}.")
        if mode is CatalogPolicyMode.PER_ARTIFACT and per_artifact_enforcer is None:
            raise ValueError("Per-artifact policy requires a backend-specific CatalogPolicyEnforcer.")
        if mode is CatalogPolicyMode.HOMOGENEOUS_SOURCE and per_artifact_enforcer is not None:
            raise ValueError("A per-artifact enforcer cannot be used with homogeneous-source policy.")
        self._provider = provider
        self._authorizer = authorizer
        self._mode = mode
        self._per_artifact_enforcer = per_artifact_enforcer

    @property
    def policy_mode(self) -> CatalogPolicyMode:
        """Return the configured enforcement granularity."""
        return self._mode

    async def capabilities(self, context: RequestContext) -> tuple[SourceCapabilities, ...]:
        """Return provider support intersected with current caller policy."""
        effective: list[SourceCapabilities] = []
        for capability in await self._provider_capabilities():
            allowed_operations: set[CatalogOperation] = set()
            for operation in capability.supported_operations:
                if await self._authorize_source(operation, capability.source_id, context):
                    allowed_operations.add(operation)
            allowed = frozenset(allowed_operations)
            if allowed:
                effective.append(SourceCapabilities(capability.source_id, allowed))
        return tuple(effective)

    async def search(self, request: SearchRequest, context: RequestContext) -> Page[CatalogArtifact]:
        """Search with authorization enforced before effective pagination."""
        constrained, allowed_sources = await self._constrain_request(request, CatalogOperation.SEARCH, context)
        if not allowed_sources:
            return Page(())
        if self._uses_per_artifact_enforcement():
            assert self._per_artifact_enforcer is not None
            page = await self._per_artifact_enforcer.search(
                self._provider,
                constrained,
                context,
                self._authorizer,
            )
        elif self._mode is CatalogPolicyMode.HOMOGENEOUS_SOURCE:
            page = await self._provider.search(constrained, context)
        else:
            self._raise_enforcement_failure(CatalogOperation.SEARCH)
        await self._validate_page(page, allowed_sources, CatalogOperation.SEARCH, context)
        return page

    async def list(self, request: ListRequest, context: RequestContext) -> Page[CatalogArtifact]:
        """List with authorization enforced before effective pagination."""
        constrained, allowed_sources = await self._constrain_request(request, CatalogOperation.LIST, context)
        if not allowed_sources:
            return Page(())
        if self._uses_per_artifact_enforcement():
            assert self._per_artifact_enforcer is not None
            page = await self._per_artifact_enforcer.list(
                self._provider,
                constrained,
                context,
                self._authorizer,
            )
        elif self._mode is CatalogPolicyMode.HOMOGENEOUS_SOURCE:
            page = await self._provider.list(constrained, context)
        else:
            self._raise_enforcement_failure(CatalogOperation.LIST)
        await self._validate_page(page, allowed_sources, CatalogOperation.LIST, context)
        return page

    async def get(self, reference: ArtifactReference, context: RequestContext) -> CatalogArtifact:
        """Get an artifact without distinguishing denied from absent artifacts."""
        await self._require_source_operation(reference, CatalogOperation.GET, context)
        try:
            if self._uses_per_artifact_enforcement():
                assert self._per_artifact_enforcer is not None
                artifact = await self._per_artifact_enforcer.get(
                    self._provider,
                    reference,
                    context,
                    self._authorizer,
                )
            elif self._mode is CatalogPolicyMode.HOMOGENEOUS_SOURCE:
                artifact = await self._provider.get(reference, context)
            else:
                self._raise_enforcement_failure(CatalogOperation.GET)
        except ArtifactNotFoundError:
            pass
        else:
            self._validate_reference(artifact.reference, reference, CatalogOperation.GET)
            if self._uses_per_artifact_enforcement():
                await self._require_artifact_operation(artifact.reference, CatalogOperation.GET, context)
            return artifact
        self._raise_not_found(CatalogOperation.GET)

    async def resolve(self, reference: ArtifactReference, context: RequestContext) -> ResolvedArtifact:
        """Resolve an artifact without distinguishing denied from absent artifacts."""
        await self._require_source_operation(reference, CatalogOperation.RESOLVE, context)
        try:
            if self._uses_per_artifact_enforcement():
                assert self._per_artifact_enforcer is not None
                resolved = await self._per_artifact_enforcer.resolve(
                    self._provider,
                    reference,
                    context,
                    self._authorizer,
                )
            elif self._mode is CatalogPolicyMode.HOMOGENEOUS_SOURCE:
                resolved = await self._provider.resolve(reference, context)
            else:
                self._raise_enforcement_failure(CatalogOperation.RESOLVE)
        except ArtifactNotFoundError:
            pass
        else:
            self._validate_reference(resolved.reference, reference, CatalogOperation.RESOLVE)
            if self._uses_per_artifact_enforcement():
                await self._require_artifact_operation(resolved.reference, CatalogOperation.RESOLVE, context)
            return resolved
        self._raise_not_found(CatalogOperation.RESOLVE)

    async def _provider_capabilities(self) -> tuple[SourceCapabilities, ...]:
        capabilities = await self._provider.capabilities()
        source_ids = [capability.source_id for capability in capabilities]
        if len(source_ids) != len(set(source_ids)):
            raise PermissionDeniedError("Catalog authorization could not be enforced.")
        return capabilities

    async def _authorize_source(
        self,
        operation: CatalogOperation,
        source_id: str,
        context: RequestContext,
    ) -> bool:
        return await self._authorizer.authorize(
            CatalogAuthorizationRequest(operation=operation, source_id=source_id),
            context,
        )

    async def _constrain_request(
        self,
        request: RequestT,
        operation: CatalogOperation,
        context: RequestContext,
    ) -> tuple[RequestT, frozenset[str]]:
        capabilities = await self._provider_capabilities()
        supported_sources = tuple(
            capability.source_id for capability in capabilities if operation in capability.supported_operations
        )
        permitted_sources = {
            source_id for source_id in supported_sources if await self._authorize_source(operation, source_id, context)
        }
        requested_sources = request.source_ids or supported_sources
        source_ids = tuple(source_id for source_id in requested_sources if source_id in permitted_sources)
        if request.page.cursor is not None and source_ids != requested_sources:
            raise InvalidRequestError(
                "Catalog cursor cannot be reused after authorization narrows the source scope.",
                operation=operation.value,
            )
        return replace(request, source_ids=source_ids), frozenset(source_ids)

    async def _require_source_operation(
        self,
        reference: ArtifactReference,
        operation: CatalogOperation,
        context: RequestContext,
    ) -> None:
        if not await self._authorize_source(operation, reference.source_id, context):
            self._raise_not_found(operation)
        capabilities = {capability.source_id: capability for capability in await self._provider_capabilities()}
        capability = capabilities.get(reference.source_id)
        if capability is None:
            self._raise_not_found(operation)
        if operation not in capability.supported_operations:
            raise UnsupportedOperationError(
                "The requested catalog operation is not supported.",
                operation=operation.value,
            )

    async def _require_artifact_operation(
        self,
        reference: ArtifactReference,
        operation: CatalogOperation,
        context: RequestContext,
    ) -> None:
        allowed = await self._authorizer.authorize(
            CatalogAuthorizationRequest(
                operation=operation,
                source_id=reference.source_id,
                reference=reference,
            ),
            context,
        )
        if not allowed:
            self._raise_not_found(operation)

    async def _validate_page(
        self,
        page: Page[CatalogArtifact],
        allowed_sources: frozenset[str],
        operation: CatalogOperation,
        context: RequestContext,
    ) -> None:
        for artifact in page.items:
            if artifact.reference.source_id not in allowed_sources:
                raise PermissionDeniedError(
                    "Catalog authorization could not be enforced.",
                    operation=operation.value,
                )
            if self._uses_per_artifact_enforcement():
                allowed = await self._authorizer.authorize(
                    CatalogAuthorizationRequest(
                        operation=operation,
                        source_id=artifact.reference.source_id,
                        reference=artifact.reference,
                    ),
                    context,
                )
                if not allowed:
                    self._raise_enforcement_failure(operation)

    @staticmethod
    def _validate_reference(
        actual: ArtifactReference,
        requested: ArtifactReference,
        operation: CatalogOperation,
    ) -> None:
        identity_matches = actual.source_id == requested.source_id and actual.artifact_id == requested.artifact_id
        requested_revision = getattr(requested, "revision", None)
        revision_matches = requested_revision is None or getattr(actual, "revision", None) == requested_revision
        if not identity_matches or not revision_matches:
            AuthorizedCatalogProvider._raise_enforcement_failure(operation)

    def _uses_per_artifact_enforcement(self) -> bool:
        if self._mode is CatalogPolicyMode.PER_ARTIFACT:
            return True
        if self._mode is CatalogPolicyMode.HOMOGENEOUS_SOURCE:
            return False
        self._raise_enforcement_failure()

    @staticmethod
    def _raise_enforcement_failure(operation: CatalogOperation | None = None) -> Never:
        raise PermissionDeniedError(
            "Catalog authorization could not be enforced.",
            operation=operation.value if operation is not None else None,
        )

    @staticmethod
    def _raise_not_found(operation: CatalogOperation) -> Never:
        raise ArtifactNotFoundError("Artifact not found.", operation=operation.value) from None
