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
    azure_uri_from_blob_name,
    canonicalize_azure_uri,
    normalize_logical_path,
    sanitize_uri_for_display,
)
from agora_workbench.data_lake.catalog import CatalogDB
from agora_workbench.data_lake.execution import AssetFetcher, AssetPublisher, DataLakeDataManager
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


def test_artifact_reference_can_follow_current_or_pin_revision():
    current = ArtifactReference("wind-hourly", source_id="weather")
    pinned = ArtifactReference("wind-hourly", source_id="weather", revision=3)

    assert current.is_current
    assert not pinned.is_current


def test_azure_uri_canonicalization_strips_credentials_and_preserves_object_case():
    expected = "az://account/container/Folder/File~Name.csv"
    assert (
        canonicalize_azure_uri(
            "https://ACCOUNT.blob.core.windows.net/container/Folder/File%7EName.csv?sv=secret#fragment"
        )
        == expected
    )
    assert (
        canonicalize_azure_uri("abfss://container@account.dfs.core.windows.net/Folder/File~Name.csv?sig=secret")
        == expected
    )
    assert canonicalize_azure_uri("az://account/container/Folder/File~Name.csv?sig=secret") == expected
    assert canonicalize_azure_uri("az://account/container/folder/File~Name.csv") != expected


def test_sdk_decoded_blob_names_are_quoted_exactly_once():
    literal_escape = azure_uri_from_blob_name("account", "container", "literal%41.csv")
    decoded_name = azure_uri_from_blob_name("account", "container", "literalA.csv")
    assert literal_escape == "az://account/container/literal%2541.csv"
    assert decoded_name == "az://account/container/literalA.csv"
    assert literal_escape != decoded_name
    assert canonicalize_azure_uri("az://account/container/literal%2541.csv") == literal_escape
    assert canonicalize_azure_uri("az://account/container/literal%41.csv") == decoded_name


def test_abfss_display_sanitization_drops_passwords():
    assert (
        sanitize_uri_for_display(
            "abfss://container:DO_NOT_LOG@account123.dfs.core.windows.net/path?sig=DO_NOT_LOG#fragment"
        )
        == "abfss://container@account123.dfs.core.windows.net/path"
    )


@pytest.mark.parametrize("container", ["$root", "$web", "$logs"])
def test_azure_system_containers_are_canonicalized(container):
    assert canonicalize_azure_uri(f"https://account.blob.core.windows.net/{container}/File.csv") == (
        f"az://account/{container}/File.csv"
    )


def test_abfss_container_is_decoded_exactly_once():
    assert canonicalize_azure_uri("abfss://%24root@account.dfs.core.windows.net/File.csv") == (
        "az://account/$root/File.csv"
    )
    with pytest.raises(InvalidRequestError):
        canonicalize_azure_uri("abfss://%2524root@account.dfs.core.windows.net/File.csv")


def test_logical_path_normalization_is_relative_and_safe():
    assert normalize_logical_path(r"folder\child\..\file.csv") == "folder/file.csv"
    with pytest.raises(InvalidRequestError):
        normalize_logical_path("../../outside.csv")


def test_generic_data_lake_error_is_internal_not_backend_unavailable():
    assert DataLakeError("unclassified").code is DataLakeErrorCode.INTERNAL


def test_resource_lease_makes_cleanup_ownership_explicit():
    credential = object()

    assert ResourceLease(credential).ownership is ResourceOwnership.BORROWED
    assert not ResourceLease(credential).should_close
    assert ResourceLease(credential, ResourceOwnership.OWNED).should_close


def test_existing_resolver_protocol_is_the_public_protocol():
    from agora_workbench.code_execution.data_access import ArtifactResolver as LegacyArtifactResolver
    from agora_workbench.data_lake.protocols import ArtifactResolver as ProtocolModuleArtifactResolver

    assert LegacyArtifactResolver is ArtifactResolver
    assert ProtocolModuleArtifactResolver is ArtifactResolver
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


def test_contract_import_does_not_initialize_runtime_or_cloud_modules():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "import agora_workbench.data_lake as data_lake; "
                "assert data_lake.__name__ == 'agora_workbench.data_lake'; "
                "assert not hasattr(data_lake, '__getattr__'); "
                "assert 'agora_workbench.code_execution' not in sys.modules; "
                "assert 'agora_workbench.data_lake.catalog' not in sys.modules; "
                "assert 'agora_workbench.data_lake.execution' not in sys.modules; "
                "assert 'agora_workbench.data_lake.resolvers' not in sys.modules; "
                "assert not any(name == 'azure' or name.startswith('azure.') for name in sys.modules)"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "imports",
    [
        (
            "from agora_workbench import CodeExecutionServer as RootCodeExecutionServer; "
            "from agora_workbench.code_execution.server import CodeExecutionServer as Implementation; "
            "assert RootCodeExecutionServer is Implementation"
        ),
        (
            "from agora_workbench.code_execution.server import CodeExecutionServer as Implementation; "
            "from agora_workbench import CodeExecutionServer as RootCodeExecutionServer; "
            "assert RootCodeExecutionServer is Implementation"
        ),
        (
            "import agora_workbench.data_lake; "
            "from agora_workbench import CodeExecutionServer as RootCodeExecutionServer; "
            "from agora_workbench.code_execution.server import CodeExecutionServer as Implementation; "
            "assert RootCodeExecutionServer is Implementation"
        ),
    ],
)
def test_root_compatibility_exports_are_stable_across_import_order(imports):
    result = subprocess.run(
        [sys.executable, "-c", imports],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_explicit_compatibility_submodules_preserve_object_identity():
    from agora_workbench.code_execution.data_access.fetchers import AssetFetcher as LegacyAssetFetcher
    from agora_workbench.code_execution.data_access.manager import DataLakeDataManager as LegacyDataLakeDataManager
    from agora_workbench.code_execution.data_access.publishers import AssetPublisher as LegacyAssetPublisher
    from agora_workbench.code_execution.data_access.artifact_resolvers import (
        SearchIndexArtifactResolver as LegacySearchIndexArtifactResolver,
    )
    from agora_workbench.data_lake import catalog, execution, resolvers

    assert catalog.CatalogDB is CatalogDB
    assert resolvers.ArtifactResolver is ArtifactResolver
    assert resolvers.SearchIndexArtifactResolver is LegacySearchIndexArtifactResolver
    assert execution.AssetFetcher is AssetFetcher is LegacyAssetFetcher
    assert execution.AssetPublisher is AssetPublisher is LegacyAssetPublisher
    assert execution.DataLakeDataManager is DataLakeDataManager is LegacyDataLakeDataManager
