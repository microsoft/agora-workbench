"""Contract and compatibility tests for ``agora_workbench.data_lake``."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import FrozenInstanceError

import pytest

from agora_workbench.data_lake import (
    MAX_PAGE_LIMIT,
    READ_OPERATIONS,
    ArtifactPresentation,
    ArtifactReference,
    ArtifactResolver,
    CatalogArtifact,
    CatalogDB,
    CatalogOperation,
    CatalogProvider,
    InvalidRequestError,
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
from agora_workbench.data_lake.errors import DataLakeError, DataLakeErrorCode


class MemoryCatalog:
    """Minimal public catalog provider with no framework or session dependency."""

    def __init__(self, artifact: CatalogArtifact):
        self.artifact = artifact
        self.contexts: list[RequestContext] = []

    async def capabilities(self) -> tuple[SourceCapabilities, ...]:
        return (SourceCapabilities("weather", READ_OPERATIONS),)

    async def search(self, request: SearchRequest, context: RequestContext) -> Page[CatalogArtifact]:
        self.contexts.append(context)
        items = (self.artifact,) if request.query in self.artifact.presentation.name else ()
        return Page(items)

    async def list(self, request: ListRequest, context: RequestContext) -> Page[CatalogArtifact]:
        self.contexts.append(context)
        return Page((self.artifact,))

    async def get(self, reference: ArtifactReference, context: RequestContext) -> CatalogArtifact:
        self.contexts.append(context)
        assert reference == self.artifact.reference
        return self.artifact

    async def resolve(self, reference: ArtifactReference, context: RequestContext) -> ResolvedArtifact:
        self.contexts.append(context)
        assert self.artifact.locator is not None
        return ResolvedArtifact(reference, self.artifact.locator)


class MemoryResolver:
    """Existing resolver shape implemented structurally."""

    @property
    def unavailable_reason(self) -> str | None:
        return None

    async def resolve(self, artifact_id: str) -> str:
        return f"file:///data/{artifact_id}"


@pytest.fixture
def artifact() -> CatalogArtifact:
    return CatalogArtifact(
        reference=ArtifactReference("wind-hourly", source_id="weather"),
        presentation=ArtifactPresentation("hourly-wind.parquet", media_type="application/x-parquet"),
        locator=StorageLocator("file:///data/hourly-wind.parquet"),
    )


async def test_custom_catalog_implements_public_contract_and_propagates_context(artifact):
    provider = MemoryCatalog(artifact)
    context = RequestContext(request_id="request-1", caller_id="caller-1")

    assert isinstance(provider, CatalogProvider)
    assert await provider.capabilities() == (SourceCapabilities("weather", READ_OPERATIONS),)
    assert (await provider.search(SearchRequest("wind", source_ids=("weather",)), context)).items == (artifact,)
    assert (await provider.list(ListRequest(source_ids=("weather",)), context)).items == (artifact,)
    assert await provider.get(artifact.reference, context) == artifact
    assert (await provider.resolve(artifact.reference, context)).locator == artifact.locator
    assert all(seen is context for seen in provider.contexts)


def test_capabilities_are_source_scoped_provider_support_only():
    capabilities = SourceCapabilities("weather", frozenset({CatalogOperation.SEARCH}))

    assert capabilities.source_id == "weather"
    assert capabilities.supports(CatalogOperation.SEARCH)
    assert not capabilities.supports(CatalogOperation.GET)
    assert not hasattr(capabilities, "caller_permitted")


def test_frozen_mapping_fields_are_copied_and_immutable():
    attributes = {"tenant": "one"}
    filters = {"format": "parquet"}
    metadata = {"region": "west"}

    context = RequestContext(attributes=attributes)
    request = SearchRequest("wind", filters=filters)
    artifact = CatalogArtifact(
        reference=ArtifactReference("wind-hourly", source_id="weather"),
        presentation=ArtifactPresentation("hourly-wind.parquet"),
        metadata=metadata,
    )
    attributes["tenant"] = "two"
    filters["format"] = "csv"
    metadata["region"] = "east"

    assert context.attributes == {"tenant": "one"}
    assert request.filters == {"format": "parquet"}
    assert artifact.metadata == {"region": "west"}
    with pytest.raises(TypeError):
        context.attributes["tenant"] = "two"  # type: ignore[index]
    assert type(context).__hash__ is None
    with pytest.raises(FrozenInstanceError):
        request.source_ids = ("other",)  # type: ignore[misc]


def test_page_request_uses_pagination_operation_and_conservative_limit():
    assert PageRequest(limit=MAX_PAGE_LIMIT).limit == MAX_PAGE_LIMIT
    with pytest.raises(InvalidRequestError) as too_small:
        PageRequest(limit=0)
    with pytest.raises(InvalidRequestError) as too_large:
        PageRequest(limit=MAX_PAGE_LIMIT + 1)
    assert too_small.value.operation == "pagination"
    assert too_large.value.operation == "pagination"


def test_generic_data_lake_error_is_internal_not_backend_unavailable():
    assert DataLakeError("unclassified").code is DataLakeErrorCode.INTERNAL


def test_resource_lease_makes_cleanup_ownership_explicit():
    credential = object()

    assert ResourceLease(credential).ownership is ResourceOwnership.BORROWED
    assert not ResourceLease(credential).should_close
    assert ResourceLease(credential, ResourceOwnership.OWNED).should_close


def test_existing_resolver_protocol_is_the_public_protocol():
    from agora_workbench.code_execution.data_access import ArtifactResolver as LegacyArtifactResolver

    assert LegacyArtifactResolver is ArtifactResolver
    assert isinstance(MemoryResolver(), ArtifactResolver)


def test_existing_catalog_classes_are_reexported_without_duplication():
    from agora_workbench.code_execution.data_access.catalog import CatalogDB as LegacyCatalogDB

    assert CatalogDB is LegacyCatalogDB
    db = CatalogDB(db_path=":memory:", vec_dimensions=4)
    db.open()
    try:
        db.upsert_artifact(
            artifact_id="artifact-1",
            name="artifact.csv",
            storage_uri="/data/artifact.csv",
            indexed_at="2026-01-01T00:00:00Z",
        )
        assert db.get_artifact("artifact-1").name == "artifact.csv"
    finally:
        db.close()


def test_existing_top_level_imports_remain_available():
    from agora_workbench import CodeExecutionServer as RootCodeExecutionServer
    from agora_workbench.code_execution import CodeExecutionServer
    from agora_workbench.code_execution.server import CodeExecutionServer as Implementation

    assert RootCodeExecutionServer is Implementation
    assert CodeExecutionServer is Implementation


def test_known_package_attributes_are_lazy_and_order_independent():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import agora_workbench; "
                "assert agora_workbench.base.__name__ == 'agora_workbench.base'; "
                "assert agora_workbench.code_execution.__name__ == 'agora_workbench.code_execution'; "
                "assert agora_workbench.data_lake.__name__ == 'agora_workbench.data_lake'; "
                "assert agora_workbench.code_execution.data_access.__name__.endswith('.data_access'); "
                "assert 'base' in dir(agora_workbench); "
                "assert 'data_access' in dir(agora_workbench.code_execution)"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_contract_import_does_not_eagerly_import_execution_or_cloud_modules():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import agora_workbench.data_lake.models; "
                "assert 'agora_workbench.code_execution' not in sys.modules; "
                "assert 'azure.search.documents.aio' not in sys.modules"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_catalog_import_does_not_eagerly_import_server_modules():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; from agora_workbench.data_lake import CatalogDB; "
                "assert CatalogDB.__name__ == 'CatalogDB'; "
                "assert 'agora_workbench.code_execution.server' not in sys.modules; "
                "assert 'agora_workbench.code_execution.sessions.session' not in sys.modules"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_direct_compatibility_submodules_are_lazy():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "import agora_workbench.data_lake.catalog as catalog; "
                "import agora_workbench.data_lake.resolvers as resolvers; "
                "assert 'agora_workbench.code_execution' not in sys.modules; "
                "assert 'azure.search.documents.aio' not in sys.modules; "
                "assert catalog.CatalogDB.__name__ == 'CatalogDB'; "
                "assert 'agora_workbench.code_execution.server' not in sys.modules; "
                "assert 'agora_workbench.code_execution.sessions.session' not in sys.modules; "
                "from agora_workbench.data_lake.protocols import ArtifactResolver; "
                "assert resolvers.ArtifactResolver is ArtifactResolver"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
