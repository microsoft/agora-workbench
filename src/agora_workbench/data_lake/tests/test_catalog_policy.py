"""Caller-aware catalog policy composition tests."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

import pytest

from agora_workbench.data_lake import (
    READ_OPERATIONS,
    ArtifactNotFoundError,
    ArtifactPresentation,
    ArtifactReference,
    AuthorizedCatalogProvider,
    CatalogArtifact,
    CatalogAuthorizationRequest,
    CatalogOperation,
    CatalogPolicyMode,
    DevelopmentAllowAllCatalogAuthorizer,
    InvalidRequestError,
    ListRequest,
    Page,
    PageRequest,
    PermissionDeniedError,
    PolicyEnforcedCatalog,
    RequestContext,
    ResolvedArtifact,
    SearchRequest,
    SourceCapabilities,
    StorageLocator,
)


def _artifact(source_id: str, artifact_id: str, name: str, domain: str) -> CatalogArtifact:
    return CatalogArtifact(
        reference=ArtifactReference(artifact_id=artifact_id, source_id=source_id),
        presentation=ArtifactPresentation(name),
        locator=StorageLocator(f"memory://{source_id}/{artifact_id}"),
        metadata={"domain": domain},
    )


class MemoryCatalog:
    def __init__(self) -> None:
        self.artifacts = (
            _artifact("public-data", "restricted-first", "forecast restricted", "weather"),
            _artifact("public-data", "open-second", "forecast open", "weather"),
            _artifact("research-data", "research-only", "forecast research", "climate"),
        )
        self.contexts: list[RequestContext] = []
        self.requests: list[SearchRequest | ListRequest] = []

    async def capabilities(self) -> tuple[SourceCapabilities, ...]:
        return (
            SourceCapabilities("public-data", READ_OPERATIONS),
            SourceCapabilities("research-data", READ_OPERATIONS),
        )

    async def search(self, request: SearchRequest, context: RequestContext) -> Page[CatalogArtifact]:
        self.contexts.append(context)
        self.requests.append(request)
        matches = [
            artifact
            for artifact in self.artifacts
            if artifact.reference.source_id in request.source_ids
            and request.query.lower() in artifact.presentation.name.lower()
        ]
        return self._page(matches, request.page)

    async def list(self, request: ListRequest, context: RequestContext) -> Page[CatalogArtifact]:
        self.contexts.append(context)
        self.requests.append(request)
        matches = [artifact for artifact in self.artifacts if artifact.reference.source_id in request.source_ids]
        return self._page(matches, request.page)

    async def get(self, reference: ArtifactReference, context: RequestContext) -> CatalogArtifact:
        self.contexts.append(context)
        for artifact in self.artifacts:
            if artifact.reference == reference:
                return artifact
        raise ArtifactNotFoundError(
            f"Unknown artifact: {reference.artifact_id}",
            resource_id=reference.artifact_id,
            operation="get",
        )

    async def resolve(self, reference: ArtifactReference, context: RequestContext) -> ResolvedArtifact:
        artifact = await self.get(reference, context)
        assert artifact.locator is not None
        return ResolvedArtifact(artifact.reference, artifact.locator)

    @staticmethod
    def _page(artifacts: list[CatalogArtifact], page: PageRequest) -> Page[CatalogArtifact]:
        start = int(page.cursor or 0)
        end = start + page.limit
        next_cursor = str(end) if end < len(artifacts) else None
        return Page(tuple(artifacts[start:end]), next_cursor)


class PrincipalPolicy:
    def __init__(
        self,
        source_access: dict[str, set[str]],
        artifact_access: dict[str, set[tuple[str, str]]],
    ) -> None:
        self.source_access = source_access
        self.artifact_access = artifact_access
        self.contexts: list[RequestContext] = []
        self.requests: list[CatalogAuthorizationRequest] = []

    async def authorize(self, request: CatalogAuthorizationRequest, context: RequestContext) -> bool:
        self.contexts.append(context)
        self.requests.append(request)
        caller_id = context.caller_id or ""
        if request.source_id not in self.source_access.get(caller_id, set()):
            return False
        if request.reference is None:
            return True
        return (
            request.reference.source_id,
            request.reference.artifact_id,
        ) in self.artifact_access.get(caller_id, set())


class MemoryPerArtifactEnforcer:
    """Test adapter that filters the complete candidate set before paging."""

    async def search(self, provider, request, context, authorizer):
        assert isinstance(provider, MemoryCatalog)
        candidates = [
            artifact
            for artifact in provider.artifacts
            if artifact.reference.source_id in request.source_ids
            and request.query.lower() in artifact.presentation.name.lower()
        ]
        return await self._authorized_page(candidates, request.page, request, context, authorizer)

    async def list(self, provider, request, context, authorizer):
        assert isinstance(provider, MemoryCatalog)
        candidates = [artifact for artifact in provider.artifacts if artifact.reference.source_id in request.source_ids]
        return await self._authorized_page(candidates, request.page, request, context, authorizer)

    async def get(self, provider, reference, context, authorizer):
        assert isinstance(provider, MemoryCatalog)
        artifact = next((item for item in provider.artifacts if item.reference == reference), None)
        if artifact is None or not await authorizer.authorize(
            CatalogAuthorizationRequest(CatalogOperation.GET, reference.source_id, reference),
            context,
        ):
            raise ArtifactNotFoundError("Artifact not found.", operation="get")
        return artifact

    async def resolve(self, provider, reference, context, authorizer):
        artifact = await self.get(provider, reference, context, authorizer)
        if not await authorizer.authorize(
            CatalogAuthorizationRequest(CatalogOperation.RESOLVE, artifact.reference.source_id, artifact.reference),
            context,
        ):
            raise ArtifactNotFoundError("Artifact not found.", operation="resolve")
        assert artifact.locator is not None
        return ResolvedArtifact(artifact.reference, artifact.locator)

    @staticmethod
    async def _authorized_page(candidates, page, request, context, authorizer):
        operation = CatalogOperation.SEARCH if isinstance(request, SearchRequest) else CatalogOperation.LIST
        authorized = []
        for artifact in candidates:
            if await authorizer.authorize(
                CatalogAuthorizationRequest(operation, artifact.reference.source_id, artifact.reference),
                context,
            ):
                authorized.append(artifact)
        start = int(page.cursor or 0)
        end = start + page.limit
        next_cursor = str(end) if end < len(authorized) else None
        return Page(tuple(authorized[start:end]), next_cursor)


@pytest.fixture
def contexts():
    return (
        RequestContext(request_id="request-a", caller_id="analyst-a", attributes={"session": "session-a"}),
        RequestContext(request_id="request-b", caller_id="analyst-b", attributes={"session": "session-b"}),
    )


async def test_homogeneous_source_policy_covers_capabilities_search_list_get_and_resolve(contexts):
    provider = MemoryCatalog()
    policy = PrincipalPolicy(
        source_access={"analyst-a": {"public-data"}, "analyst-b": {"research-data"}},
        artifact_access={},
    )
    catalog = AuthorizedCatalogProvider(
        provider,
        policy,
        mode=CatalogPolicyMode.HOMOGENEOUS_SOURCE,
    )
    analyst_a, analyst_b = contexts

    assert [item.source_id for item in await catalog.capabilities(analyst_a)] == ["public-data"]
    assert [item.source_id for item in await catalog.capabilities(analyst_b)] == ["research-data"]
    assert isinstance(catalog, PolicyEnforcedCatalog)
    assert {
        item.reference.source_id for item in (await catalog.search(SearchRequest("forecast"), analyst_a)).items
    } == {"public-data"}
    assert {item.reference.source_id for item in (await catalog.list(ListRequest(), analyst_b)).items} == {
        "research-data"
    }
    reference = ArtifactReference("open-second", "public-data")
    assert await catalog.get(reference, analyst_a) == provider.artifacts[1]
    assert (await catalog.resolve(reference, analyst_a)).reference == reference
    with pytest.raises(ArtifactNotFoundError, match="Artifact not found") as denied:
        await catalog.get(reference, analyst_b)
    assert denied.value.resource_id is None
    assert all(context in contexts for context in provider.contexts)
    assert all(context in contexts for context in policy.contexts)
    assert provider.requests[0].source_ids == ("public-data",)
    assert provider.requests[1].source_ids == ("research-data",)


async def test_per_artifact_policy_filters_before_top_k_and_hides_denied_lookups(contexts):
    provider = MemoryCatalog()
    policy = PrincipalPolicy(
        source_access={"analyst-a": {"public-data"}, "analyst-b": {"public-data"}},
        artifact_access={
            "analyst-a": {("public-data", "open-second")},
            "analyst-b": {("public-data", "restricted-first")},
        },
    )
    catalog = AuthorizedCatalogProvider(
        provider,
        policy,
        mode=CatalogPolicyMode.PER_ARTIFACT,
        per_artifact_enforcer=MemoryPerArtifactEnforcer(),
    )
    analyst_a, analyst_b = contexts
    top_one = PageRequest(limit=1)

    assert (await catalog.search(SearchRequest("forecast", page=top_one), analyst_a)).items[
        0
    ].reference.artifact_id == ("open-second")
    assert (await catalog.search(SearchRequest("forecast", page=top_one), analyst_b)).items[
        0
    ].reference.artifact_id == "restricted-first"
    assert (await catalog.list(ListRequest(page=top_one), analyst_a)).items[0].reference.artifact_id == "open-second"

    reference = ArtifactReference("open-second", "public-data")
    assert await catalog.get(reference, analyst_a) == provider.artifacts[1]
    assert (await catalog.resolve(reference, analyst_a)).reference == reference
    with pytest.raises(ArtifactNotFoundError) as denied:
        await catalog.get(reference, analyst_b)
    with pytest.raises(ArtifactNotFoundError) as missing:
        await catalog.get(ArtifactReference("missing", "public-data"), analyst_b)
    assert str(denied.value) == str(missing.value) == "Artifact not found."
    assert denied.value.resource_id is None
    assert missing.value.resource_id is None


@pytest.mark.parametrize("operation", ["get", "resolve"])
@pytest.mark.parametrize(
    "returned_reference",
    [
        pytest.param(ArtifactReference("open-second", "research-data"), id="source-mismatch"),
        pytest.param(ArtifactReference("different-artifact", "public-data"), id="artifact-id-mismatch"),
    ],
)
@pytest.mark.parametrize("mode", [CatalogPolicyMode.HOMOGENEOUS_SOURCE, CatalogPolicyMode.PER_ARTIFACT])
async def test_get_and_resolve_fail_closed_on_complete_identity_mismatch(
    contexts,
    operation,
    returned_reference,
    mode,
):
    class MismatchedCatalog(MemoryCatalog):
        async def get(self, reference, context):
            return replace(self.artifacts[1], reference=returned_reference)

        async def resolve(self, reference, context):
            assert self.artifacts[1].locator is not None
            return ResolvedArtifact(returned_reference, self.artifacts[1].locator)

    class MismatchedEnforcer(MemoryPerArtifactEnforcer):
        async def get(self, provider, reference, context, authorizer):
            return replace(provider.artifacts[1], reference=returned_reference)

        async def resolve(self, provider, reference, context, authorizer):
            assert provider.artifacts[1].locator is not None
            return ResolvedArtifact(returned_reference, provider.artifacts[1].locator)

    provider = MismatchedCatalog()
    policy = PrincipalPolicy(
        source_access={"analyst-a": {"public-data"}},
        artifact_access={
            "analyst-a": {
                ("public-data", "open-second"),
                (returned_reference.source_id, returned_reference.artifact_id),
            }
        },
    )
    catalog = AuthorizedCatalogProvider(
        provider,
        policy,
        mode=mode,
        per_artifact_enforcer=MismatchedEnforcer() if mode is CatalogPolicyMode.PER_ARTIFACT else None,
    )

    with pytest.raises(PermissionDeniedError, match="could not be enforced") as error:
        await getattr(catalog, operation)(ArtifactReference("open-second", "public-data"), contexts[0])
    assert error.value.operation == operation


async def test_source_intersection_preserves_cursor_and_rejects_out_of_scope_results(contexts):
    provider = MemoryCatalog()
    catalog = AuthorizedCatalogProvider(
        provider,
        PrincipalPolicy({"analyst-a": {"public-data"}}, {}),
        mode=CatalogPolicyMode.HOMOGENEOUS_SOURCE,
    )

    await catalog.list(
        ListRequest(
            source_ids=("public-data",),
            page=PageRequest(limit=1, cursor="1"),
        ),
        contexts[0],
    )

    assert provider.requests[-1].source_ids == ("public-data",)
    assert provider.requests[-1].page.cursor == "1"


async def test_cursor_fails_closed_when_policy_narrows_source_scope(contexts):
    policy = PrincipalPolicy({"analyst-a": {"public-data", "research-data"}}, {})
    catalog = AuthorizedCatalogProvider(
        MemoryCatalog(),
        policy,
        mode=CatalogPolicyMode.HOMOGENEOUS_SOURCE,
    )
    request = ListRequest(
        source_ids=("public-data", "research-data"),
        page=PageRequest(limit=1),
    )
    first_page = await catalog.list(request, contexts[0])
    assert first_page.next_cursor is not None
    policy.source_access["analyst-a"].remove("research-data")

    with pytest.raises(InvalidRequestError, match="resume with explicit source_ids") as error:
        await catalog.list(
            replace(request, page=PageRequest(limit=1, cursor=first_page.next_cursor)),
            contexts[0],
        )
    assert error.value.operation == "list"


async def test_cursor_fails_closed_when_provider_capabilities_narrow_source_scope(contexts):
    class SearchOnlyResearchCatalog(MemoryCatalog):
        async def capabilities(self):
            return (
                SourceCapabilities("public-data", READ_OPERATIONS),
                SourceCapabilities("research-data", frozenset({CatalogOperation.SEARCH})),
            )

    catalog = AuthorizedCatalogProvider(
        SearchOnlyResearchCatalog(),
        DevelopmentAllowAllCatalogAuthorizer(),
        mode=CatalogPolicyMode.HOMOGENEOUS_SOURCE,
    )

    with pytest.raises(InvalidRequestError, match="provider capabilities or authorization") as error:
        await catalog.list(
            ListRequest(
                source_ids=("public-data", "research-data"),
                page=PageRequest(limit=1, cursor="1"),
            ),
            contexts[0],
        )
    assert error.value.operation == "list"


async def test_search_authorizes_only_the_requested_operation(contexts):
    policy = PrincipalPolicy({"analyst-a": {"public-data"}}, {})
    catalog = AuthorizedCatalogProvider(
        MemoryCatalog(),
        policy,
        mode=CatalogPolicyMode.HOMOGENEOUS_SOURCE,
    )

    await catalog.search(SearchRequest("forecast"), contexts[0])

    assert {request.operation for request in policy.requests} == {CatalogOperation.SEARCH}


@pytest.mark.parametrize("operation", ["get", "resolve"])
@pytest.mark.parametrize("mode", [CatalogPolicyMode.HOMOGENEOUS_SOURCE, CatalogPolicyMode.PER_ARTIFACT])
async def test_detailed_backend_not_found_is_normalized(contexts, operation, mode):
    detailed = ArtifactNotFoundError(
        "Secret backend object customer-42 was absent.",
        resource_id="customer-42",
        operation=operation,
    )

    class MissingCatalog(MemoryCatalog):
        async def get(self, reference, context):
            raise detailed from RuntimeError("backend detail")

        async def resolve(self, reference, context):
            raise detailed from RuntimeError("backend detail")

    class MissingEnforcer(MemoryPerArtifactEnforcer):
        async def get(self, provider, reference, context, authorizer):
            raise detailed from RuntimeError("enforcer detail")

        async def resolve(self, provider, reference, context, authorizer):
            raise detailed from RuntimeError("enforcer detail")

    catalog = AuthorizedCatalogProvider(
        MissingCatalog(),
        PrincipalPolicy(
            {"analyst-a": {"public-data"}},
            {"analyst-a": {("public-data", "open-second")}},
        ),
        mode=mode,
        per_artifact_enforcer=MissingEnforcer() if mode is CatalogPolicyMode.PER_ARTIFACT else None,
    )

    with pytest.raises(ArtifactNotFoundError) as error:
        await getattr(catalog, operation)(ArtifactReference("open-second", "public-data"), contexts[0])
    assert str(error.value) == "Artifact not found."
    assert error.value.resource_id is None
    assert error.value.operation == operation
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


@pytest.mark.parametrize("operation", ["get", "resolve"])
@pytest.mark.parametrize("mode", [CatalogPolicyMode.HOMOGENEOUS_SOURCE, CatalogPolicyMode.PER_ARTIFACT])
async def test_backend_permission_denial_is_normalized(contexts, operation, mode):
    detailed = PermissionDeniedError(
        "Caller cannot access secret backend object customer-42.",
        resource_id="customer-42",
        operation=operation,
    )

    class DenyingCatalog(MemoryCatalog):
        async def get(self, reference, context):
            raise detailed from RuntimeError("backend detail")

        async def resolve(self, reference, context):
            raise detailed from RuntimeError("backend detail")

    class DenyingEnforcer(MemoryPerArtifactEnforcer):
        async def get(self, provider, reference, context, authorizer):
            raise detailed from RuntimeError("enforcer detail")

        async def resolve(self, provider, reference, context, authorizer):
            raise detailed from RuntimeError("enforcer detail")

    catalog = AuthorizedCatalogProvider(
        DenyingCatalog(),
        PrincipalPolicy(
            {"analyst-a": {"public-data"}},
            {"analyst-a": {("public-data", "open-second")}},
        ),
        mode=mode,
        per_artifact_enforcer=DenyingEnforcer() if mode is CatalogPolicyMode.PER_ARTIFACT else None,
    )

    with pytest.raises(PermissionDeniedError) as error:
        await getattr(catalog, operation)(ArtifactReference("open-second", "public-data"), contexts[0])
    assert str(error.value) == "Catalog authorization could not be enforced."
    assert error.value.resource_id is None
    assert error.value.operation == operation
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def test_unknown_policy_mode_fails_closed():
    class FuturePolicyMode(StrEnum):
        FUTURE = "future"

    with pytest.raises(ValueError, match="Unknown catalog policy mode"):
        AuthorizedCatalogProvider(
            MemoryCatalog(),
            DevelopmentAllowAllCatalogAuthorizer(),
            mode=FuturePolicyMode.FUTURE,  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class RevisionedReference(ArtifactReference):
    revision: str | None = None


@pytest.mark.parametrize("operation", ["get", "resolve"])
async def test_requested_revision_must_match_returned_revision(contexts, operation):
    requested = RevisionedReference("open-second", "public-data", revision="revision-1")
    returned = RevisionedReference("open-second", "public-data", revision="revision-2")

    class RevisionCatalog(MemoryCatalog):
        async def get(self, reference, context):
            return replace(self.artifacts[1], reference=returned)

        async def resolve(self, reference, context):
            assert self.artifacts[1].locator is not None
            return ResolvedArtifact(returned, self.artifacts[1].locator)

    catalog = AuthorizedCatalogProvider(
        RevisionCatalog(),
        DevelopmentAllowAllCatalogAuthorizer(),
        mode=CatalogPolicyMode.HOMOGENEOUS_SOURCE,
    )

    with pytest.raises(PermissionDeniedError, match="could not be enforced"):
        await getattr(catalog, operation)(requested, contexts[0])


@pytest.mark.parametrize("operation", ["get", "resolve"])
async def test_follow_current_reference_accepts_concrete_returned_revision(contexts, operation):
    requested = RevisionedReference("open-second", "public-data", revision=None)
    returned = RevisionedReference("open-second", "public-data", revision="revision-2")

    class RevisionCatalog(MemoryCatalog):
        async def get(self, reference, context):
            return replace(self.artifacts[1], reference=returned)

        async def resolve(self, reference, context):
            assert self.artifacts[1].locator is not None
            return ResolvedArtifact(returned, self.artifacts[1].locator)

    catalog = AuthorizedCatalogProvider(
        RevisionCatalog(),
        DevelopmentAllowAllCatalogAuthorizer(),
        mode=CatalogPolicyMode.HOMOGENEOUS_SOURCE,
    )

    result = await getattr(catalog, operation)(requested, contexts[0])
    assert result.reference == returned


async def test_unauthorized_enforcer_page_is_an_enforcement_failure(contexts):
    class LeakyEnforcer(MemoryPerArtifactEnforcer):
        async def search(self, provider, request, context, authorizer):
            return Page((provider.artifacts[0],))

    catalog = AuthorizedCatalogProvider(
        MemoryCatalog(),
        PrincipalPolicy(
            {"analyst-a": {"public-data"}},
            {"analyst-a": {("public-data", "open-second")}},
        ),
        mode=CatalogPolicyMode.PER_ARTIFACT,
        per_artifact_enforcer=LeakyEnforcer(),
    )

    with pytest.raises(PermissionDeniedError) as error:
        await catalog.search(SearchRequest("forecast"), contexts[0])
    assert str(error.value) == "Catalog authorization could not be enforced."
    assert error.value.resource_id is None


async def test_policy_is_rechecked_per_request_for_context_isolation_and_revocation(contexts):
    provider = MemoryCatalog()
    policy = PrincipalPolicy(
        source_access={"analyst-a": {"public-data"}, "analyst-b": set()},
        artifact_access={},
    )
    catalog = AuthorizedCatalogProvider(provider, policy, mode=CatalogPolicyMode.HOMOGENEOUS_SOURCE)
    analyst_a, analyst_b = contexts

    assert (await catalog.list(ListRequest(), analyst_a)).items
    assert not (await catalog.list(ListRequest(), analyst_b)).items
    policy.source_access["analyst-a"].clear()
    assert not (await catalog.list(ListRequest(), analyst_a)).items
    assert policy.contexts[-1] is analyst_a


async def test_effective_capabilities_intersect_provider_support_and_policy(contexts):
    class SearchOnlyPolicy:
        async def authorize(self, request, context):
            return context.caller_id == "analyst-a" and request.operation is CatalogOperation.SEARCH

    catalog = AuthorizedCatalogProvider(
        MemoryCatalog(),
        SearchOnlyPolicy(),
        mode=CatalogPolicyMode.HOMOGENEOUS_SOURCE,
    )

    capabilities = await catalog.capabilities(contexts[0])
    assert capabilities == (
        SourceCapabilities("public-data", frozenset({CatalogOperation.SEARCH})),
        SourceCapabilities("research-data", frozenset({CatalogOperation.SEARCH})),
    )
    assert await catalog.capabilities(contexts[1]) == ()


def test_per_artifact_mode_has_no_implicit_post_filter_fallback():
    with pytest.raises(ValueError, match="requires a backend-specific"):
        AuthorizedCatalogProvider(
            MemoryCatalog(),
            DevelopmentAllowAllCatalogAuthorizer(),
            mode=CatalogPolicyMode.PER_ARTIFACT,
        )


async def test_provider_constraint_violation_fails_closed(contexts):
    class MisbehavingCatalog(MemoryCatalog):
        async def list(self, request, context):
            return Page((self.artifacts[-1],))

    catalog = AuthorizedCatalogProvider(
        MisbehavingCatalog(),
        PrincipalPolicy({"analyst-a": {"public-data"}}, {}),
        mode=CatalogPolicyMode.HOMOGENEOUS_SOURCE,
    )

    with pytest.raises(PermissionDeniedError, match="could not be enforced"):
        await catalog.list(ListRequest(), contexts[0])
