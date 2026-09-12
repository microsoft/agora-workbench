"""End-to-end tests for opt-in code-execution catalog integration."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from agora_workbench.code_execution import CatalogIntegration, CodeExecutionServer, ServerConfig
from agora_workbench.code_execution.auth import create_noop_auth_config
from agora_workbench.code_execution.catalog_integration import (
    _decode_reference,
    _encode_reference,
    _error_payload,
    register_catalog_discovery_tools,
)
from agora_workbench.code_execution.catalog_tools import (
    CatalogToolsContext,
    register_catalog_admin_tools,
    register_catalog_tools,
)
from agora_workbench.code_execution.data_access.fetchers import AssetFetcher
from agora_workbench.code_execution.data_access.manager import DataLakeDataManager
from agora_workbench.code_execution.sessions import (
    SessionConfig,
    SessionContext,
    SessionManager,
    set_current_request_token,
    set_current_token_claims,
    set_current_user_identity,
)
from agora_workbench.data_lake import (
    ArtifactNotFoundError,
    ArtifactPresentation,
    ArtifactReference,
    CatalogArtifact,
    CatalogOperation,
    CatalogPolicyMode,
    Page,
    ResourceLease,
    ResourceOwnership,
    RequestContext,
    ResolvedArtifact,
    SearchRequest,
    SourceCapabilities,
    StorageLocator,
    stable_source_id,
)
from agora_workbench.data_lake.catalog import CatalogConfig, DiscoveryMode, SourceConfig
from agora_workbench.code_execution.data_access.catalog import CatalogDB


class _PerUserAuthorizer:
    def __init__(self, allowed_source: str):
        self.allowed_source = allowed_source

    async def authorize(self, request, context):
        return request.source_id == self.allowed_source and context.caller_id is not None


def _server_config(tmp_path: Path) -> ServerConfig:
    return ServerConfig(
        name="catalog-test",
        description="Catalog test server",
        type="uv",
        dependency_file="[project]\nname='catalog-test'\nversion='0.0.0'\n",
        build_dir=tmp_path / "env",
        auto_build=False,
    )


def _write_manifest(root: Path, source_id: str, filename: str, artifact_id: str) -> SourceConfig:
    root.mkdir()
    (root / filename).write_text(f"{source_id}-payload")
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "generation": 1,
                "artifacts": [
                    {
                        "path": filename,
                        "artifact_id": artifact_id,
                        "domain": source_id,
                        "media_type": "text/plain",
                    }
                ],
            }
        )
    )
    return SourceConfig(
        source_id=source_id,
        path=str(root),
        discovery=DiscoveryMode.MANIFEST,
        manifest="manifest.json",
    )


async def test_no_catalog_preserves_session_factory_and_tool_surface(tmp_path):
    factory = lambda _context: DataLakeDataManager()  # noqa: E731
    session_manager = SessionManager(SessionConfig(data_manager_factory=factory))
    server = CodeExecutionServer(
        _server_config(tmp_path),
        auth_config=create_noop_auth_config(),
        session_manager=session_manager,
    )

    assert session_manager.config.data_manager_factory is factory
    tool_names = {tool.name for tool in await server.mcp.list_tools()}
    assert "search_data" not in tool_names
    assert "get_catalog_capabilities" not in tool_names
    session_manager.aclose_all_sessions = AsyncMock()
    server._sidecar_manager.stop_all = AsyncMock()
    server._close_tool_search_backends = AsyncMock()
    server.activity_publisher.stop = AsyncMock()
    await server._shutdown()
    session_manager.aclose_all_sessions.assert_awaited_once()


def test_data_manager_preserves_positional_artifact_resolver():
    resolver = cast(Any, SimpleNamespace(resolve=AsyncMock(), unavailable_reason="unavailable"))
    credential = cast(Any, SimpleNamespace(close=AsyncMock(), get_token=AsyncMock()))
    manager = DataLakeDataManager([], [], credential, resolver)
    try:
        assert manager._artifact_resolver is resolver
        assert manager._owns_credential is False
    finally:
        manager.cleanup()


@pytest.mark.parametrize(
    "payload",
    [
        {"artifact_id": ["not", "a", "string"], "source_id": "source"},
        {"artifact_id": "artifact", "source_id": {"not": "a string"}},
        {"artifact_id": "artifact", "source_id": "source", "revision": 1.5},
        {"artifact_id": "artifact", "source_id": "source", "revision": True},
    ],
)
def test_catalog_reference_rejects_invalid_field_types(payload):
    import base64

    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    with pytest.raises(ValueError, match="invalid"):
        _decode_reference(f"catalog-v1:{encoded}")


def test_catalog_error_payload_sanitizes_uri_resource_id():
    error = ArtifactNotFoundError(
        "Artifact not found.",
        resource_id="https://user:secret@example.test/data?sig=secret#fragment",
        operation="get",
    )

    assert _error_payload(error)["resource_id"] == "https://example.test/data"


async def test_configured_catalog_uses_stable_fallback_source_id(tmp_path):
    root = tmp_path / "implicit-source"
    root.mkdir()
    (root / "data.txt").write_text("payload")
    integration = CatalogIntegration.development_from_config(CatalogConfig(sources=[SourceConfig(path=str(root))]))

    await integration.startup()
    try:
        expected_source_id = stable_source_id("local", str(root.resolve()))
        assert [capability.source_id for capability in await integration.provider.capabilities()] == [
            expected_source_id
        ]
        page = await integration.provider.search(SearchRequest(""), RequestContext())
        assert page.items[0].reference.source_id == expected_source_id
    finally:
        await integration.shutdown()


async def test_configured_catalog_startup_rejects_unready_source(tmp_path):
    integration = CatalogIntegration.development_from_config(
        CatalogConfig(sources=[SourceConfig(path=str(tmp_path / "missing"))]),
        db_path=tmp_path / "catalog.db",
    )

    with pytest.raises(RuntimeError, match="not ready"):
        await integration.startup()

    assert cast(Any, integration.provider)._closed


def test_from_config_rejects_invalid_authorizer_before_opening_database(tmp_path):
    source_root = tmp_path / "source"
    source_root.mkdir()
    database = tmp_path / "catalog.db"
    config = CatalogConfig(sources=[SourceConfig(path=str(source_root))])

    with pytest.raises(ValueError, match="exactly one"):
        CatalogIntegration.from_config(config, db_path=database)

    assert not database.exists()


async def test_server_startup_failure_rolls_back_owned_catalog(tmp_path):
    provider = _LifecycleProvider()
    integration = CatalogIntegration(
        ResourceLease(provider, ResourceOwnership.OWNED),
        authorizer=_PerUserAuthorizer("source"),
    )
    server = CodeExecutionServer(
        _server_config(tmp_path),
        auth_config=create_noop_auth_config(),
        catalog=integration,
    )
    server._ensure_environment = AsyncMock(side_effect=RuntimeError("environment failed"))
    with pytest.raises(RuntimeError, match="environment failed"):
        await server._startup()
    assert (provider.load_calls, provider.close_calls) == (1, 1)


async def test_server_shutdown_cancellation_still_closes_sessions_and_catalog(tmp_path):
    provider = _LifecycleProvider()
    integration = CatalogIntegration(
        ResourceLease(provider, ResourceOwnership.OWNED),
        authorizer=_PerUserAuthorizer("source"),
        load_on_startup=False,
    )
    server = CodeExecutionServer(
        _server_config(tmp_path),
        auth_config=create_noop_auth_config(),
        catalog=integration,
    )
    await integration.startup()
    sidecar_started = asyncio.Event()
    sidecar_gate = asyncio.Event()
    sidecar_stopped = asyncio.Event()

    async def stop_sidecars():
        sidecar_started.set()
        await sidecar_gate.wait()
        sidecar_stopped.set()

    server._sidecar_manager.stop_all = stop_sidecars
    server._close_tool_search_backends = AsyncMock(side_effect=RuntimeError("tool close failed"))
    session_cleanup = AsyncMock()
    server.session_manager.aclose_all_sessions = session_cleanup

    shutdown = asyncio.create_task(server._shutdown())
    await sidecar_started.wait()
    shutdown.cancel()
    await asyncio.sleep(0)
    assert not shutdown.done()
    sidecar_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await shutdown

    assert sidecar_stopped.is_set()
    session_cleanup.assert_awaited_once()
    assert provider.close_calls == 1


async def test_two_sessions_isolate_policy_and_resolve_catalog_references(tmp_path):
    first_source = _write_manifest(tmp_path / "alice", "alice-source", "alice.txt", "alice-artifact")
    second_source = _write_manifest(tmp_path / "bob", "bob-source", "bob.txt", "bob-artifact")
    integration = CatalogIntegration.from_config(
        CatalogConfig(sources=[first_source, second_source]),
        authorizer_factory=lambda context: _PerUserAuthorizer(context.user_identity.split("@", 1)[0] + "-source"),
        policy_mode=CatalogPolicyMode.HOMOGENEOUS_SOURCE,
    )
    server = CodeExecutionServer(
        _server_config(tmp_path),
        auth_config=create_noop_auth_config(),
        catalog=integration,
    )
    await integration.startup()
    try:
        alice_id = server.session_manager.create_session(
            {}, user_identity="alice@tenant", user_token="alice-token", token_claims={"tenant": "a"}
        )
        bob_id = server.session_manager.create_session(
            {}, user_identity="bob@tenant", user_token="bob-token", token_claims={"tenant": "b"}
        )
        alice = server.session_manager.get_session(alice_id)
        bob = server.session_manager.get_session(bob_id)
        alice_binding = alice.extensions["catalog"]
        bob_binding = bob.extensions["catalog"]

        alice_page = await alice_binding.catalog.search(
            SearchRequest(""),
            alice_binding.context,
        )
        bob_page = await bob_binding.catalog.search(
            SearchRequest(""),
            bob_binding.context,
        )

        assert [item.reference.source_id for item in alice_page.items] == ["alice-source"]
        assert [item.reference.source_id for item in bob_page.items] == ["bob-source"]
        assert alice.data_manager is not bob.data_manager
        assert alice.data_manager._credential is not bob.data_manager._credential
        assert alice_binding.context.attributes["claims"] == {"tenant": "a"}
        assert bob_binding.context.attributes["claims"] == {"tenant": "b"}

        reference = ArtifactReference("alice-artifact", "alice-source", alice_page.items[0].revision)
        cached = await alice.data_manager.get_cache_path(f"<blob>{_encode_reference(reference)}</blob>")
        assert cached.read_text() == "alice-source-payload"
    finally:
        await server.session_manager.aclose_all_sessions()
        await integration.shutdown()


async def test_blob_reference_resolves_and_streams_through_session_manager(tmp_path):
    artifact = CatalogArtifact(
        ArtifactReference("blob-artifact", "blob-source"),
        ArtifactPresentation("blob.csv", media_type="text/csv"),
        StorageLocator("az://account/container/blob.csv"),
        revision=1,
    )

    class BlobProvider:
        async def capabilities(self):
            return (SourceCapabilities("blob-source", frozenset(CatalogOperation)),)

        async def search(self, request, context):
            return Page((artifact,))

        async def list(self, request, context):
            return Page((artifact,))

        async def get(self, reference, context):
            return artifact

        async def resolve(self, reference, context):
            assert artifact.locator is not None
            return ResolvedArtifact(reference, artifact.locator)

    class BlobFetcher:
        __slots__ = ("destination",)

        def __init__(self):
            self.destination = None

        def can_handle(self, qualified_name):
            return qualified_name.startswith("az://")

        async def fetch_to_file(self, qualified_name, dest_path):
            self.destination = dest_path
            dest_path.write_text("blob-payload")
            return len("blob-payload")

        async def close(self):
            return None

    integration = CatalogIntegration(
        ResourceLease(BlobProvider()),
        authorizer=_PerUserAuthorizer("blob-source"),
    )
    credentials = []

    class CredentialProvider:
        def __init__(self, token):
            self.token = token
            self.close_calls = 0

        async def get_token(self, scope):
            return SimpleNamespace(token=self.token, expires_on=9999999999)

        async def close(self):
            self.close_calls += 1

    auth = create_noop_auth_config()

    def credential_factory(token):
        provider = CredentialProvider(token)
        credentials.append(provider)
        return provider

    auth.credential_provider_factory = credential_factory
    server = CodeExecutionServer(
        _server_config(tmp_path),
        auth_config=auth,
        catalog=integration,
    )
    await integration.startup()
    try:
        session_id = server.session_manager.create_session({}, "user", "token", {})
        session = server.session_manager.get_session(session_id)
        session.data_manager._fetchers.insert(0, cast(AssetFetcher, BlobFetcher()))
        binding = session.extensions["catalog"]
        page = await binding.catalog.search(SearchRequest("blob"), binding.context)
        reference = ArtifactReference("blob-artifact", "blob-source", page.items[0].revision)
        encoded_reference = _encode_reference(reference)
        initial_path = await session.data_manager.get_cache_path(f"<blob>{encoded_reference}</blob>")
        assert initial_path.read_text() == "blob-payload"
        assert encoded_reference in session.data_manager._cache_index
        set_current_request_token("refreshed-token")
        try:
            server._refresh_session_token(session)
        finally:
            set_current_request_token(None)
        assert encoded_reference not in session.data_manager._cache_index
        refreshed_access_token = await session.data_manager._credential.get_token("scope")
        assert refreshed_access_token.token == "refreshed-token"
        path = await session.data_manager.get_cache_path(f"<blob>{encoded_reference}</blob>")
        assert path.read_text() == "blob-payload"
        legacy_fetcher = session.data_manager._fetchers[0]
        assert legacy_fetcher.destination != path
        assert ".legacy-part" in legacy_fetcher.destination.name
        assert not legacy_fetcher.destination.exists()
    finally:
        await server.session_manager.aclose_all_sessions()
        await integration.shutdown()
    assert [credential.token for credential in credentials] == ["token", "refreshed-token"]
    assert credentials[0].close_calls == 1
    assert credentials[1].close_calls == 1


async def test_catalog_cache_refresh_does_not_publish_in_flight_stale_fetch():
    started = asyncio.Event()
    gate = asyncio.Event()
    resolve_calls = 0
    fetch_calls = 0

    class Resolver:
        unavailable_reason = None

        async def resolve(self, artifact_id):
            nonlocal resolve_calls
            resolve_calls += 1
            return "az://account/container/blob.csv"

    class Fetcher:
        def can_handle(self, qualified_name):
            return qualified_name.startswith("az://")

        async def fetch_to_file(self, qualified_name, dest_path):
            nonlocal fetch_calls
            fetch_calls += 1
            if fetch_calls == 1:
                started.set()
                await gate.wait()
            dest_path.write_text(f"fetch-{fetch_calls}")
            return dest_path.stat().st_size

    manager = DataLakeDataManager(
        extra_fetchers=[cast(AssetFetcher, Fetcher())],
        artifact_resolver=cast(Any, Resolver()),
    )
    reference = "<blob>catalog-v1:opaque</blob>"
    fetch = asyncio.create_task(manager.get_cache_path(reference))
    await started.wait()

    manager.invalidate_cache_entries(artifact_id_prefix="catalog-v1:")
    gate.set()
    path = await fetch

    assert path.read_text() == "fetch-2"
    assert resolve_calls == 2
    assert fetch_calls == 2
    await manager.aclose()


async def test_custom_manager_factory_and_resolver_are_preserved(tmp_path):
    class CustomResolver:
        unavailable_reason = None

        async def resolve(self, artifact_id: str) -> str:
            return artifact_id

    resolver = CustomResolver()
    managers: list[DataLakeDataManager] = []

    def manager_factory(_context):
        manager = DataLakeDataManager(artifact_resolver=resolver)
        managers.append(manager)
        return manager

    source = _write_manifest(tmp_path / "source", "source", "data.txt", "artifact")
    integration = CatalogIntegration.development_from_config(CatalogConfig(sources=[source]))
    session_manager = SessionManager(SessionConfig(data_manager_factory=manager_factory))
    CodeExecutionServer(
        _server_config(tmp_path),
        auth_config=create_noop_auth_config(),
        session_manager=session_manager,
        catalog=integration,
    )

    session_id = session_manager.create_session({}, "user", "token", {})
    session = session_manager.get_session(session_id)
    assert session.data_manager is managers[0]
    assert session.data_manager._artifact_resolver is resolver
    assert not session.extensions["catalog"].execution_references
    await session_manager.aclose_all_sessions()
    await integration.shutdown()


class _LifecycleProvider:
    def __init__(self, *, fail: BaseException | None = None):
        self.fail = fail
        self.load_calls = 0
        self.close_calls = 0

    async def load(self):
        self.load_calls += 1
        if self.fail is not None:
            raise self.fail

    async def capabilities(self):
        return (SourceCapabilities("source", frozenset(CatalogOperation)),)

    async def search(self, request, context):
        return Page(())

    async def list(self, request, context):
        return Page(())

    async def get(self, reference, context):
        raise AssertionError

    async def resolve(self, reference, context):
        raise AssertionError

    async def aclose(self):
        self.close_calls += 1


async def test_owned_and_borrowed_catalog_lifecycle():
    owned_provider = _LifecycleProvider()
    owned = CatalogIntegration(
        ResourceLease(owned_provider, ResourceOwnership.OWNED),
        authorizer=_PerUserAuthorizer("source"),
    )
    await owned.startup()
    await owned.shutdown()
    assert (owned_provider.load_calls, owned_provider.close_calls) == (1, 1)

    borrowed_provider = _LifecycleProvider()
    borrowed = CatalogIntegration(
        ResourceLease(borrowed_provider, ResourceOwnership.BORROWED),
        authorizer=_PerUserAuthorizer("source"),
    )
    await borrowed.startup()
    await borrowed.shutdown()
    assert (borrowed_provider.load_calls, borrowed_provider.close_calls) == (0, 0)


async def test_refreshed_session_token_updates_catalog_request_context_and_authorizer(tmp_path):
    provider = _LifecycleProvider()
    authorizers = []

    class ClaimsAuthorizer:
        def __init__(self, claims):
            self.role = claims.get("role")
            self.close_calls = 0
            authorizers.append(self)

        async def authorize(self, request, context):
            return self.role == "writer"

        async def aclose(self):
            self.close_calls += 1

    integration = CatalogIntegration(
        ResourceLease(provider, ResourceOwnership.BORROWED),
        authorizer_factory=lambda context: ClaimsAuthorizer(context.token_claims),
        load_on_startup=False,
    )
    server = CodeExecutionServer(
        _server_config(tmp_path),
        auth_config=create_noop_auth_config(),
        catalog=integration,
    )
    session_id = server.session_manager.create_session(
        {},
        user_identity="user",
        user_token="old-token",
        token_claims={"role": "reader"},
    )
    session = server.session_manager.get_session(session_id)

    set_current_request_token("new-token")
    set_current_token_claims({"role": "writer"})
    try:
        server._refresh_session_token(session)
    finally:
        set_current_request_token(None)
        set_current_token_claims(None)

    binding = session.extensions["catalog"]
    assert binding.context.attributes["claims"] == {"role": "writer"}
    assert binding.resolver._context is binding.context
    assert (await binding.catalog.capabilities(binding.context))[0].source_id == "source"

    server._verify_session_ownership = AsyncMock(return_value=True)

    def fail_refresh(context):
        del context
        raise ValueError("credential refresh failed")

    binding.add_context_refresher(fail_refresh)
    set_current_request_token("failed-token")
    set_current_token_claims({"role": "reader"})
    set_current_user_identity("user")
    try:
        with pytest.raises(ValueError, match="credential refresh failed"):
            await server._get_or_create_session("search_data", session_id=session_id)
    finally:
        set_current_request_token(None)
        set_current_token_claims(None)
        set_current_user_identity(None)

    assert session.user_token == "new-token"
    assert server.session_manager.get_session(session_id) is session
    assert binding.context.attributes["claims"] == {"role": "writer"}
    assert (await binding.catalog.capabilities(binding.context))[0].source_id == "source"

    await server.session_manager.aclose_all_sessions()
    await integration.shutdown()
    assert [authorizer.close_calls for authorizer in authorizers] == [1, 1, 1]


@pytest.mark.parametrize("failure", [RuntimeError("load failed"), asyncio.CancelledError()])
async def test_owned_catalog_startup_failure_and_cancellation_close(failure):
    provider = _LifecycleProvider(fail=failure)
    integration = CatalogIntegration(
        ResourceLease(provider, ResourceOwnership.OWNED),
        authorizer=_PerUserAuthorizer("source"),
    )
    with pytest.raises(type(failure)):
        await integration.startup()
    assert provider.close_calls == 1


async def test_discovery_tools_keep_payload_shape_and_enforce_bounds():
    artifact = CatalogArtifact(
        ArtifactReference("artifact", "source", revision=3),
        ArtifactPresentation(
            "data.csv",
            description="See https://example.test/data.csv?sig=secret",
            media_type="text/csv; source=https://example.test/type?sig=secret",
            size_bytes=4,
        ),
        StorageLocator("file:///data/data.csv"),
        metadata={
            "domain": "science https://example.test/domain?sig=secret",
            "source_type": "local",
            "id": "metadata-id",
            "source_id": "metadata-source",
            "current_revision": 99,
            "load_path": "<blob>untrusted</blob>",
            "storage_uri": "https://user:secret@example.test/data.csv?sig=secret",
            "documentation_url": "https://user:secret@example.test/docs?sig=secret#section",
            "related": {
                "url": "https://example.test/related?sig=secret",
                "see https://example.test/key?sig=secret": "safe",
            },
        },
        score=0.75,
        revision=2,
    )
    catalog = SimpleNamespace(
        search=AsyncMock(return_value=Page((artifact,))),
        get=AsyncMock(return_value=artifact),
        list=AsyncMock(return_value=Page((artifact,))),
        resolve=AsyncMock(return_value=ResolvedArtifact(artifact.reference, cast(StorageLocator, artifact.locator))),
        capabilities=AsyncMock(
            return_value=(
                SourceCapabilities(
                    "source",
                    frozenset(
                        {
                            CatalogOperation.SEARCH,
                            CatalogOperation.LIST,
                            CatalogOperation.GET,
                            CatalogOperation.RESOLVE,
                        }
                    ),
                ),
            )
        ),
    )
    binding = SimpleNamespace(catalog=catalog, context=object(), execution_references=True)
    session = SimpleNamespace(data_manager=SimpleNamespace(), extensions={"catalog": binding})
    captured = {}
    fake_server = SimpleNamespace(
        mcp=SimpleNamespace(
            tool=lambda name, description: lambda function: captured.setdefault(name, function),
        ),
        _get_or_create_session=AsyncMock(return_value=session),
    )

    async def effective_capabilities(current):
        return await current.catalog.capabilities()

    integration = SimpleNamespace(
        capabilities=AsyncMock(side_effect=effective_capabilities),
        _policy_mode=CatalogPolicyMode.HOMOGENEOUS_SOURCE,
    )
    register_catalog_discovery_tools(fake_server, cast(CatalogIntegration, integration))

    result = await captured["search_data"]("data")
    assert result[0]["id"] == "artifact"
    assert result[0]["source_id"] == "source"
    assert result[0]["current_revision"] == 2
    assert "storage_uri" not in result[0]
    assert result[0]["documentation_url"] == "https://example.test/docs"
    assert result[0]["related"] == {
        "url": "https://example.test/related",
        "see https://example.test/key": "safe",
    }
    assert result[0]["description"] == "See https://example.test/data.csv"
    assert result[0]["content_type"] == "text/csv; source=https://example.test/type"
    assert result[0]["score"] == 0.75
    assert result[0]["load_path"].startswith("<blob>catalog-v1:")
    encoded_reference = result[0]["load_path"].removeprefix("<blob>").removesuffix("</blob>")
    assert _decode_reference(encoded_reference).revision == 3
    assert catalog.resolve.await_count == 0
    assert catalog.capabilities.await_count == 1
    details = await captured["get_artifact"]("artifact")
    assert details["current_revision"] == 2
    assert await captured["list_domains"]() == ["science https://example.test/domain"]

    catalog.capabilities.return_value = (SourceCapabilities("source", frozenset({CatalogOperation.SEARCH})),)
    integration.capabilities.return_value = (
        SourceCapabilities("source", frozenset({CatalogOperation.SEARCH, CatalogOperation.RESOLVE})),
    )
    capabilities = await captured["get_catalog_capabilities"]()
    assert not capabilities["execution_references"]


async def test_source_less_get_uses_unique_authorized_match_and_rejects_ambiguity():
    first = CatalogArtifact(
        ArtifactReference("shared-id", "first"),
        ArtifactPresentation("first.csv"),
        score=0.5,
    )
    second = CatalogArtifact(
        ArtifactReference("shared-id", "second"),
        ArtifactPresentation("second.csv"),
    )
    matches = {"first": first}

    async def get(reference, context):
        del context
        artifact = matches.get(reference.source_id)
        if artifact is None:
            from agora_workbench.data_lake import ArtifactNotFoundError

            raise ArtifactNotFoundError("Artifact not found.", operation="get")
        return artifact

    catalog = SimpleNamespace(
        get=AsyncMock(side_effect=get),
        capabilities=AsyncMock(
            return_value=(
                SourceCapabilities("first", frozenset({CatalogOperation.GET})),
                SourceCapabilities("second", frozenset({CatalogOperation.GET})),
            )
        ),
    )
    binding = SimpleNamespace(catalog=catalog, context=object(), execution_references=False)
    captured = {}
    fake_server = SimpleNamespace(
        mcp=SimpleNamespace(tool=lambda name, description: lambda function: captured.setdefault(name, function)),
        _get_or_create_session=AsyncMock(
            return_value=SimpleNamespace(data_manager=SimpleNamespace(), extensions={"catalog": binding})
        ),
    )

    async def effective_capabilities(current):
        return await current.catalog.capabilities()

    integration = SimpleNamespace(
        capabilities=AsyncMock(side_effect=effective_capabilities),
        _policy_mode=CatalogPolicyMode.HOMOGENEOUS_SOURCE,
    )
    register_catalog_discovery_tools(fake_server, cast(CatalogIntegration, integration))

    unique = await captured["get_artifact"]("shared-id")
    assert unique["source_id"] == "first"
    assert unique["score"] == 0.5

    matches["second"] = second
    ambiguous = await captured["get_artifact"]("shared-id")
    assert ambiguous["error_type"] == "invalid_request"
    assert "matches multiple sources" in ambiguous["error"]


async def test_catalog_tool_registration_modes_cannot_be_combined():
    captured = {}
    fake_mcp = SimpleNamespace(
        tool=lambda name, description: lambda function: captured.setdefault(name, function),
    )
    fake_server = SimpleNamespace(mcp=fake_mcp)
    register_catalog_discovery_tools(fake_server, cast(CatalogIntegration, SimpleNamespace()))
    with pytest.raises(RuntimeError, match="already registered.*policy-aware"):
        register_catalog_discovery_tools(fake_server, cast(CatalogIntegration, SimpleNamespace()))
    with pytest.raises(RuntimeError, match="policy-aware.*separate administrative"):
        register_catalog_admin_tools(cast(Any, fake_server.mcp), cast(Any, SimpleNamespace()))
    assert fake_server.mcp._agora_catalog_tool_mode == "policy-aware"

    db = CatalogDB(":memory:")
    db.open()
    try:
        legacy_mcp = SimpleNamespace(tool=lambda name, description: lambda function: function)
        register_catalog_tools(
            cast(Any, legacy_mcp),
            CatalogToolsContext(db, None, CatalogConfig()),
        )
        with pytest.raises(RuntimeError, match="legacy-unscoped.*query_catalog"):
            register_catalog_discovery_tools(
                SimpleNamespace(mcp=legacy_mcp),
                cast(CatalogIntegration, SimpleNamespace()),
            )

        admin_mcp = SimpleNamespace(tool=lambda name, description: lambda function: function)
        register_catalog_admin_tools(cast(Any, admin_mcp), CatalogToolsContext(db, None, CatalogConfig()))
        with pytest.raises(RuntimeError, match="admin"):
            register_catalog_discovery_tools(
                SimpleNamespace(mcp=admin_mcp),
                cast(CatalogIntegration, SimpleNamespace()),
            )
    finally:
        db.close()


async def test_session_capability_extension_merges_and_closes():
    class Extension:
        def __init__(self):
            self.closed = False

        async def capabilities(self, context):
            return (SourceCapabilities("source", frozenset({CatalogOperation.RESOLVE})),)

        async def aclose(self):
            self.closed = True

    extension = Extension()
    provider = _LifecycleProvider()
    integration = CatalogIntegration(
        ResourceLease(provider),
        authorizer=_PerUserAuthorizer("source"),
        capability_extension_factory=lambda context, catalog, request_context: extension,
    )
    context = SessionContext("session", "user", "token")
    binding = integration.bind_session(context, execution_references=True)

    capabilities = await integration.capabilities(binding)
    assert capabilities[0].supports(CatalogOperation.SEARCH)
    assert capabilities[0].supports(CatalogOperation.RESOLVE)
    await binding.aclose()
    assert extension.closed


async def test_cancelled_extension_cleanup_still_attempts_later_extensions():
    closed = False
    cancelled_attempts = 0

    class CancelledExtension:
        async def aclose(self):
            nonlocal cancelled_attempts
            cancelled_attempts += 1
            if cancelled_attempts == 1:
                raise asyncio.CancelledError()

    class LaterExtension:
        async def aclose(self):
            nonlocal closed
            closed = True

    integration = CatalogIntegration(
        ResourceLease(_LifecycleProvider()),
        authorizer=_PerUserAuthorizer("source"),
        capability_extension_factory=lambda context, catalog, request_context: (
            CancelledExtension(),
            LaterExtension(),
        ),
    )
    binding = integration.bind_session(SessionContext("session", "user", "token"), execution_references=True)

    with pytest.raises(asyncio.CancelledError):
        await binding.aclose()

    assert closed
    assert not binding._closed
    await binding.aclose()
    assert cancelled_attempts == 2
    assert binding._closed


async def test_sync_session_close_tracks_async_only_extension_until_shutdown(tmp_path):
    started = asyncio.Event()
    gate = asyncio.Event()

    class Extension:
        async def aclose(self):
            started.set()
            await gate.wait()

    extension = Extension()
    provider = _LifecycleProvider()
    integration = CatalogIntegration(
        ResourceLease(provider),
        authorizer=_PerUserAuthorizer("source"),
        capability_extension_factory=lambda context, catalog, request_context: extension,
    )
    server = CodeExecutionServer(
        _server_config(tmp_path),
        auth_config=create_noop_auth_config(),
        catalog=integration,
    )
    session_id = server.session_manager.create_session({}, "user", "token", {})

    server.session_manager.close_session(session_id)
    await started.wait()
    drain = asyncio.create_task(server.session_manager.aclose_all_sessions())
    await asyncio.sleep(0)
    assert not drain.done()

    gate.set()
    await drain


async def test_factory_rollback_async_extension_is_awaited_by_integration_shutdown(tmp_path):
    started = asyncio.Event()
    gate = asyncio.Event()

    class Extension:
        async def aclose(self):
            started.set()
            await gate.wait()

    extension = Extension()
    provider = _LifecycleProvider()
    integration = CatalogIntegration(
        ResourceLease(provider),
        authorizer=_PerUserAuthorizer("source"),
        capability_extension_factory=lambda context, catalog, request_context: extension,
    )
    session_manager = SessionManager(
        SessionConfig(data_manager_factory=lambda context: (_ for _ in ()).throw(RuntimeError("factory failed")))
    )
    CodeExecutionServer(
        _server_config(tmp_path),
        auth_config=create_noop_auth_config(),
        session_manager=session_manager,
        catalog=integration,
    )

    with pytest.raises(RuntimeError, match="factory failed"):
        session_manager.create_session({}, "user", "token", {})
    await started.wait()
    shutdown = asyncio.create_task(integration.shutdown())
    await asyncio.sleep(0)
    assert not shutdown.done()

    gate.set()
    await shutdown


async def test_binding_factory_failure_closes_factory_authorizer():
    authorizer = AsyncMock()
    integration = CatalogIntegration(
        ResourceLease(_LifecycleProvider()),
        authorizer_factory=lambda context: authorizer,
        capability_extension_factory=lambda context, catalog, request_context: (_ for _ in ()).throw(
            RuntimeError("extension factory failed")
        ),
    )

    with pytest.raises(RuntimeError, match="extension factory failed"):
        integration.bind_session(SessionContext("session", "user", "token"), execution_references=True)

    await integration.shutdown()
    authorizer.aclose.assert_awaited_once()


async def test_cancelled_catalog_drain_remains_tracked_for_next_shutdown():
    started = asyncio.Event()
    gate = asyncio.Event()
    finished = asyncio.Event()

    class Extension:
        async def aclose(self):
            started.set()
            await gate.wait()
            finished.set()

    integration = CatalogIntegration(
        ResourceLease(_LifecycleProvider()),
        authorizer=_PerUserAuthorizer("source"),
        capability_extension_factory=lambda context, catalog, request_context: Extension(),
    )
    binding = integration.bind_session(SessionContext("session", "user", "token"), execution_references=True)
    binding.cleanup()
    await started.wait()

    first_shutdown = asyncio.create_task(integration.shutdown())
    await asyncio.sleep(0)
    first_shutdown.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_shutdown

    second_shutdown = asyncio.create_task(integration.shutdown())
    await asyncio.sleep(0)
    assert not second_shutdown.done()
    gate.set()
    await second_shutdown
    assert finished.is_set()


async def test_catalog_cleanup_cancellation_retry_is_bounded_and_provider_closes():
    attempts = 0
    provider = _LifecycleProvider()

    class Extension:
        async def aclose(self):
            nonlocal attempts
            attempts += 1
            raise asyncio.CancelledError

    integration = CatalogIntegration(
        ResourceLease(provider, ResourceOwnership.OWNED),
        authorizer=_PerUserAuthorizer("source"),
        capability_extension_factory=lambda context, catalog, request_context: Extension(),
    )
    binding = integration.bind_session(SessionContext("session", "user", "token"), execution_references=True)
    binding.cleanup()

    with pytest.raises(asyncio.CancelledError):
        await integration.shutdown()

    assert attempts == 2
    assert provider.close_calls == 1


async def test_slots_manager_accepts_catalog_binding_without_mutation(tmp_path):
    class SlotsManager:
        __slots__ = ()

        def cleanup(self):
            return None

        async def aclose(self):
            return None

    manager = SlotsManager()
    source = _write_manifest(tmp_path / "slots-source", "source", "data.txt", "artifact")
    integration = CatalogIntegration.development_from_config(CatalogConfig(sources=[source]))
    session_manager = SessionManager(SessionConfig(data_manager_factory=lambda _context: cast(Any, manager)))
    CodeExecutionServer(
        _server_config(tmp_path),
        auth_config=create_noop_auth_config(),
        session_manager=session_manager,
        catalog=integration,
    )

    session_id = session_manager.create_session({}, "user", "token", {})
    session = session_manager.get_session(session_id)
    assert session.data_manager is manager
    assert session.extensions["catalog"].catalog is not None
    await session_manager.aclose_all_sessions()
    await integration.shutdown()
