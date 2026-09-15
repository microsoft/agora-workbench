"""End-to-end tests for opt-in code-execution catalog integration."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from agora_workbench.code_execution import CatalogIntegration, CodeExecutionServer, ServerConfig
from agora_workbench.code_execution.auth import create_noop_auth_config
from agora_workbench.code_execution.catalog_integration import (
    SessionCredential,
    _AsyncCleanupTracker,
    _ConfiguredCatalogProvider,
    _PreparedContextRefresh,
    _artifact_payload,
    _close_resources,
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
    SessionResources,
    set_current_request_token,
    set_current_token_claims,
    set_current_user_identity,
)
from agora_workbench.data_lake import (
    ArtifactNotFoundError,
    ArtifactPresentation,
    ArtifactReference,
    BackendUnavailableError,
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
    TransferOptions,
    stable_source_id,
)
from agora_workbench.data_lake.catalog import CatalogConfig, DiscoveryMode, SearchConfig, SourceConfig
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
        {"artifact_id": "artifact", "source_id": "source", "revision": 0},
        {"artifact_id": "artifact", "source_id": "source", "revision": -1},
    ],
)
def test_catalog_reference_rejects_invalid_field_types(payload):
    import base64

    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    with pytest.raises(ValueError, match="invalid"):
        _decode_reference(f"catalog-v1:{encoded}")


def test_catalog_reference_rejects_non_object_payload():
    import base64

    encoded = base64.urlsafe_b64encode(b"[]").decode().rstrip("=")
    with pytest.raises(ValueError, match="invalid"):
        _decode_reference(f"catalog-v1:{encoded}")


def test_catalog_error_payload_sanitizes_uri_resource_id():
    error = ArtifactNotFoundError(
        "Artifact not found.",
        resource_id="https://user:secret@example.test/data?sig=secret#fragment",
        operation="get",
    )

    assert _error_payload(error)["resource_id"] == "https://example.test/data"


@pytest.mark.parametrize(
    ("artifact_id", "source_id"),
    [
        ("https://user:secret@example.test/artifact?sig=secret", "source"),
        ("artifact", "https://user:secret@example.test/source?sig=secret"),
    ],
)
def test_catalog_payload_rejects_locator_shaped_identifiers(artifact_id, source_id):
    artifact = CatalogArtifact(
        ArtifactReference(artifact_id, source_id),
        ArtifactPresentation("data.csv"),
    )

    with pytest.raises(ValueError, match="logical identifier") as error:
        _artifact_payload(artifact)

    assert "secret" not in str(error.value)


@pytest.mark.parametrize("artifact_id", ["https://example.test/data", "az://container/data"])
def test_catalog_payload_rejects_plain_uri_identifiers(artifact_id):
    artifact = CatalogArtifact(
        ArtifactReference(artifact_id, "source"),
        ArtifactPresentation("data.csv"),
    )

    with pytest.raises(ValueError, match="logical identifier"):
        _artifact_payload(artifact)


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


async def test_failed_provider_close_retains_private_cache_for_shutdown_retry(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "data.txt").write_text("payload")
    integration = CatalogIntegration.development_from_config(
        CatalogConfig(sources=[SourceConfig(source_id="source", path=str(root))])
    )
    private_directory = integration._private_cache_directory
    assert private_directory is not None
    original_close = integration._close_provider

    async def fail_close():
        raise RuntimeError("provider close failed")

    integration._close_provider = fail_close
    with pytest.raises(ExceptionGroup, match="shutdown failed"):
        await integration.shutdown()

    assert private_directory.exists()
    assert integration._private_cache_directory == private_directory

    integration._close_provider = original_close
    await integration.shutdown()
    assert not private_directory.exists()
    assert integration._private_cache_directory is None


async def test_failed_private_cache_removal_is_retried(tmp_path, monkeypatch):
    integration = CatalogIntegration.development_from_config(
        CatalogConfig(sources=[SourceConfig(source_id="source", path=str(tmp_path))])
    )
    private_directory = integration._private_cache_directory
    assert private_directory is not None
    original_rmtree = shutil.rmtree
    attempts = 0

    def fail_once(path):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("busy")
        original_rmtree(path)

    monkeypatch.setattr(shutil, "rmtree", fail_once)
    with pytest.raises(ExceptionGroup, match="shutdown failed"):
        await integration.shutdown()

    assert integration._private_cache_directory == private_directory
    assert private_directory.exists()
    await integration.shutdown()
    assert integration._private_cache_directory is None
    assert attempts == 2


async def test_configured_catalog_preserves_valid_manifest_generation_after_refresh_failure(tmp_path):
    root = tmp_path / "manifest-source"
    source = _write_manifest(root, "source", "data.txt", "artifact")
    database = tmp_path / "catalog.db"
    provider = _ConfiguredCatalogProvider(CatalogConfig(sources=[source]), db_path=database)
    assert await provider.load() == 1
    await provider.aclose()

    (root / "manifest.json").write_text("{")
    replacement = _ConfiguredCatalogProvider(CatalogConfig(sources=[source]), db_path=database)
    try:
        assert await replacement.load() == 0
        artifact = await replacement.get(ArtifactReference("artifact", "source"), RequestContext())
        assert artifact.reference.artifact_id == "artifact"
    finally:
        await replacement.aclose()


async def test_configured_catalog_rejects_manifest_generation_after_stale_bound(tmp_path):
    root = tmp_path / "manifest-source"
    source = _write_manifest(root, "source", "data.txt", "artifact")
    source.max_stale_seconds = 0
    database = tmp_path / "catalog.db"
    provider = _ConfiguredCatalogProvider(CatalogConfig(sources=[source]), db_path=database)
    assert await provider.load() == 1
    await provider.aclose()

    (root / "manifest.json").write_text("{")
    replacement = _ConfiguredCatalogProvider(CatalogConfig(sources=[source]), db_path=database)
    try:
        with pytest.raises(RuntimeError, match="not ready"):
            await replacement.load()
        with pytest.raises(BackendUnavailableError, match="not ready"):
            await replacement.get(ArtifactReference("artifact", "source"), RequestContext())
    finally:
        await replacement.aclose()


async def test_configured_catalog_search_uses_query_embedding_and_hybrid_alpha(tmp_path, monkeypatch):
    root = tmp_path / "source"
    root.mkdir()
    (root / "data.txt").write_text("payload")
    config = CatalogConfig(
        sources=[SourceConfig(source_id="source", path=str(root))],
        search=SearchConfig(embedding_model="none", embedding_dimensions=2, hybrid_alpha=0.25),
    )
    provider = _ConfiguredCatalogProvider(config)

    class Embeddings:
        dimensions = 2

        async def embed(self, texts):
            return [[0.1, 0.2] for _ in texts]

    provider._indexer._embedding_provider = Embeddings()
    captured = {}
    original_search = CatalogDB.search

    def search(db, query, **kwargs):
        captured.update(kwargs)
        return original_search(db, query, **kwargs)

    monkeypatch.setattr(CatalogDB, "search", search)
    try:
        await provider.load()
        await provider.search(SearchRequest("semantic query"), RequestContext())
    finally:
        await provider.aclose()

    assert captured["query_embedding"] == [0.1, 0.2]
    assert captured["hybrid_alpha"] == 0.25


async def test_configured_keyword_only_catalog_searches_without_embeddings(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "searchable.txt").write_text("payload")
    provider = _ConfiguredCatalogProvider(CatalogConfig(sources=[SourceConfig(source_id="source", path=str(root))]))
    try:
        await provider.load()
        page = await provider.search(SearchRequest("searchable"), RequestContext())
    finally:
        await provider.aclose()

    assert [artifact.presentation.name for artifact in page.items] == ["searchable.txt"]


async def test_configured_keyword_only_catalog_uses_fts_weight(tmp_path, monkeypatch):
    root = tmp_path / "source"
    root.mkdir()
    (root / "searchable.txt").write_text("payload")
    config = CatalogConfig(
        sources=[SourceConfig(source_id="source", path=str(root))],
        search=SearchConfig(embedding_model="none", hybrid_alpha=0.0),
    )
    provider = _ConfiguredCatalogProvider(config)
    captured = {}
    original_search = CatalogDB.search

    def search(db, query, **kwargs):
        captured.update(kwargs)
        return original_search(db, query, **kwargs)

    monkeypatch.setattr(CatalogDB, "search", search)
    try:
        await provider.load()
        page = await provider.search(SearchRequest("searchable"), RequestContext())
    finally:
        await provider.aclose()

    assert [artifact.presentation.name for artifact in page.items] == ["searchable.txt"]
    assert captured["query_embedding"] is None
    assert captured["hybrid_alpha"] == 1.0


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


async def test_catalog_startup_failure_uses_server_shutdown_retry(tmp_path):
    class Provider(_LifecycleProvider):
        async def load(self):
            self.load_calls += 1
            raise RuntimeError("load failed")

        async def aclose(self):
            self.close_calls += 1
            if self.close_calls == 1:
                raise RuntimeError("close failed")

    provider = Provider()
    integration = CatalogIntegration(
        ResourceLease(provider, ResourceOwnership.OWNED),
        authorizer=_PerUserAuthorizer("source"),
    )
    server = CodeExecutionServer(
        _server_config(tmp_path),
        auth_config=create_noop_auth_config(),
        catalog=integration,
    )
    server._ensure_environment = AsyncMock()

    with pytest.raises(RuntimeError, match="load failed"):
        await server._startup()

    server._ensure_environment.assert_not_awaited()
    assert (provider.load_calls, provider.close_calls) == (1, 2)


async def test_default_data_manager_rollback_tracks_owned_credential_cleanup(tmp_path, monkeypatch):
    """A failure after the default catalog data manager is built must route its
    rollback through the manager's tracked async cleanup, not an untracked task."""
    import dataclasses

    from agora_workbench.code_execution.catalog_integration import CatalogSessionBinding

    gate = asyncio.Event()
    closed = asyncio.Event()

    class _FakeCredential:
        async def get_token(self, scope):
            raise NotImplementedError

        async def close(self):
            await gate.wait()
            closed.set()

    provider = _LifecycleProvider()
    integration = CatalogIntegration(
        ResourceLease(provider),
        authorizer=_PerUserAuthorizer("source"),
    )
    auth_config = dataclasses.replace(
        create_noop_auth_config(),
        credential_provider_factory=lambda user_token: _FakeCredential(),
    )
    server = CodeExecutionServer(
        _server_config(tmp_path),
        auth_config=auth_config,
        catalog=integration,
    )

    call_count = {"n": 0}
    original = CatalogSessionBinding.add_context_refresher

    def flaky_add_context_refresher(self, refresher):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("refresher registration failed")
        return original(self, refresher)

    monkeypatch.setattr(CatalogSessionBinding, "add_context_refresher", flaky_add_context_refresher)

    with pytest.raises(RuntimeError, match="refresher registration failed"):
        server.session_manager.create_session({}, "user", "token", {})

    drain = asyncio.create_task(server.session_manager.await_resource_cleanup())
    await asyncio.sleep(0)
    assert not drain.done()
    assert not closed.is_set()

    gate.set()
    assert await drain is None
    assert closed.is_set()


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
        _ = await shutdown

    assert sidecar_stopped.is_set()
    session_cleanup.assert_awaited_once()
    assert provider.close_calls == 1


async def test_catalog_cleanup_drains_through_repeated_cancellation():
    started = asyncio.Event()
    gate = asyncio.Event()
    finished = asyncio.Event()

    async def cleanup():
        started.set()
        await gate.wait()
        finished.set()

    drain = asyncio.create_task(CodeExecutionServer._await_catalog_cleanup(cleanup(), "test cleanup"))
    await started.wait()
    drain.cancel()
    await asyncio.sleep(0)
    assert not drain.done()
    drain.cancel()
    await asyncio.sleep(0)
    assert not drain.done()

    gate.set()
    cancelled = await drain
    assert isinstance(cancelled, asyncio.CancelledError)
    assert finished.is_set()


async def test_catalog_shutdown_drains_cleanup_before_provider_through_repeated_cancellation():
    cleanup_started = asyncio.Event()
    cleanup_gate = asyncio.Event()
    provider = _LifecycleProvider()
    integration = CatalogIntegration(
        ResourceLease(provider, ResourceOwnership.OWNED),
        authorizer=_PerUserAuthorizer("source"),
        load_on_startup=False,
    )

    async def cleanup():
        cleanup_started.set()
        await cleanup_gate.wait()

    integration._cleanup_tracker.schedule(cleanup())
    shutdown = asyncio.create_task(integration.shutdown())
    await cleanup_started.wait()
    shutdown.cancel()
    await asyncio.sleep(0)
    shutdown.cancel()
    await asyncio.sleep(0)

    assert not shutdown.done()
    assert provider.close_calls == 0
    cleanup_gate.set()
    with pytest.raises(asyncio.CancelledError):
        _ = await shutdown
    assert provider.close_calls == 1


async def test_catalog_shutdown_cancellation_does_not_close_provider_before_cleanup():
    cleanup_started = asyncio.Event()
    cleanup_gate = asyncio.Event()
    provider = _LifecycleProvider()
    integration = CatalogIntegration(
        ResourceLease(provider, ResourceOwnership.OWNED),
        authorizer=_PerUserAuthorizer("source"),
        load_on_startup=False,
    )

    async def cleanup():
        cleanup_started.set()
        await cleanup_gate.wait()

    integration._cleanup_tracker.schedule(cleanup())
    shutdown = asyncio.create_task(integration.shutdown())
    await cleanup_started.wait()
    shutdown.cancel()
    await asyncio.sleep(0)

    assert not shutdown.done()
    assert provider.close_calls == 0
    cleanup_gate.set()
    with pytest.raises(asyncio.CancelledError):
        _ = await shutdown
    assert provider.close_calls == 1


async def test_catalog_cleanup_returns_terminal_cleanup_cancellation():
    async def cleanup():
        raise asyncio.CancelledError

    cancelled = await asyncio.wait_for(
        CodeExecutionServer._await_catalog_cleanup(cleanup(), "test cleanup"),
        timeout=1,
    )

    assert isinstance(cancelled, asyncio.CancelledError)


async def test_catalog_cleanup_retries_one_ordinary_failure():
    calls = 0

    async def cleanup():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient close failure")

    cancelled = await CodeExecutionServer._await_catalog_cleanup(
        cleanup(),
        "test cleanup",
        retry=cleanup,
    )

    assert cancelled is None
    assert calls == 2


@pytest.mark.parametrize("cancelled_stage", ["tool_search", "publisher", "activity"])
async def test_server_shutdown_drains_each_cancelled_resource_once(tmp_path, cancelled_stage):
    started = asyncio.Event()
    gate = asyncio.Event()

    class CloseResource:
        def __init__(self, name, *, block=False):
            self.destination_name = name
            self.block = block
            self.close_calls = 0
            self.closed = False

        async def close(self):
            self.close_calls += 1
            if self.close_calls > 1:
                raise RuntimeError(f"{self.destination_name} closed twice")
            if self.block:
                started.set()
                await gate.wait()
            self.closed = True

    class ActivityResource:
        def __init__(self, *, block=False):
            self.block = block
            self.stop_calls = 0
            self.stopped = False

        async def stop(self):
            self.stop_calls += 1
            if self.stop_calls > 1:
                raise RuntimeError("activity publisher stopped twice")
            if self.block:
                started.set()
                await gate.wait()
            self.stopped = True

    server = CodeExecutionServer(
        _server_config(tmp_path),
        auth_config=create_noop_auth_config(),
    )
    tool_backends = [
        CloseResource("tool-blocking", block=cancelled_stage == "tool_search"),
        CloseResource("tool-later"),
    ]
    publishers = [
        CloseResource("publisher-blocking", block=cancelled_stage == "publisher"),
        CloseResource("publisher-later"),
    ]
    activity = ActivityResource(block=cancelled_stage == "activity")
    server._tool_search_backends = tool_backends
    server._publishers = cast(Any, publishers)
    server.activity_publisher = cast(Any, activity)
    server._sidecar_manager.stop_all = AsyncMock()
    server.session_manager.aclose_all_sessions = AsyncMock()

    shutdown = asyncio.create_task(server._shutdown())
    await started.wait()
    shutdown.cancel()
    await asyncio.sleep(0)
    assert not shutdown.done()
    gate.set()

    with pytest.raises(asyncio.CancelledError):
        _ = await shutdown

    assert all(resource.closed for resource in (*tool_backends, *publishers))
    assert activity.stopped
    await server._shutdown()
    assert [resource.close_calls for resource in (*tool_backends, *publishers)] == [1, 1, 1, 1]
    assert activity.stop_calls == 1


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


async def test_catalog_cache_refresh_keeps_previously_returned_path_until_session_cleanup():
    class Resolver:
        unavailable_reason = None

        async def resolve(self, artifact_id):
            return "az://account/container/blob.csv"

    class Fetcher:
        def can_handle(self, qualified_name):
            return qualified_name.startswith("az://")

        async def fetch_to_file(self, qualified_name, dest_path):
            dest_path.write_text("payload")
            return dest_path.stat().st_size

    manager = DataLakeDataManager(
        extra_fetchers=[cast(AssetFetcher, Fetcher())],
        artifact_resolver=cast(Any, Resolver()),
    )
    reference = "<blob>catalog-v1:opaque</blob>"
    cached_path = await manager.get_cache_path(reference)

    manager.invalidate_cache_entries(artifact_id_prefix="catalog-v1:")

    assert "catalog-v1:opaque" not in manager._cache_index
    assert cached_path.read_text() == "payload"
    await manager.aclose()
    assert not cached_path.exists()


async def test_repeated_cache_invalidation_has_bounded_fetch_retries():
    fetch_calls = 0
    manager = None

    class Resolver:
        unavailable_reason = None

        async def resolve(self, artifact_id):
            return "az://account/container/blob.csv"

    class Fetcher:
        def can_handle(self, qualified_name):
            return qualified_name.startswith("az://")

        async def fetch_to_file(self, qualified_name, dest_path):
            nonlocal fetch_calls
            fetch_calls += 1
            dest_path.write_text(f"fetch-{fetch_calls}")
            assert manager is not None
            manager.invalidate_cache_entries(artifact_id_prefix="catalog-v1:")
            return dest_path.stat().st_size

    manager = DataLakeDataManager(
        extra_fetchers=[cast(AssetFetcher, Fetcher())],
        artifact_resolver=cast(Any, Resolver()),
    )

    with pytest.raises(BackendUnavailableError, match="changed repeatedly"):
        await manager.get_cache_path("<blob>catalog-v1:opaque</blob>")

    assert fetch_calls == 2
    assert manager._cache_index == {}
    await manager.aclose()


async def test_catalog_cache_reauthorization_retries_after_concurrent_refresh():
    reauthorization_started = asyncio.Event()
    reauthorization_gate = asyncio.Event()
    resolve_calls = 0
    fetch_calls = 0

    class Resolver:
        unavailable_reason = None

        async def resolve(self, artifact_id):
            nonlocal resolve_calls
            resolve_calls += 1
            if resolve_calls == 2:
                reauthorization_started.set()
                await reauthorization_gate.wait()
                raise PermissionError("stale authorization")
            return f"az://account/container/blob-{resolve_calls}.csv"

    class Fetcher:
        def can_handle(self, qualified_name):
            return qualified_name.startswith("az://")

        async def fetch_to_file(self, qualified_name, dest_path):
            nonlocal fetch_calls
            fetch_calls += 1
            dest_path.write_text(f"fetch-{fetch_calls}")
            return dest_path.stat().st_size

    manager = DataLakeDataManager(
        extra_fetchers=[cast(AssetFetcher, Fetcher())],
        artifact_resolver=cast(Any, Resolver()),
    )
    reference = "<blob>catalog-v1:opaque</blob>"
    assert (await manager.get_cache_path(reference)).read_text() == "fetch-1"
    refreshed = asyncio.create_task(manager.get_cache_path(reference))
    await reauthorization_started.wait()

    manager.invalidate_cache_entries(artifact_id_prefix="catalog-v1:")
    reauthorization_gate.set()
    refreshed_path = await refreshed

    assert refreshed_path.read_text() == "fetch-2"
    assert resolve_calls == 3
    assert fetch_calls == 2
    assert manager._cache_index["catalog-v1:opaque"] == refreshed_path
    await manager.aclose()


async def test_stale_cache_validation_does_not_remove_newer_cache_entry():
    reauthorization_started = asyncio.Event()
    reauthorization_gate = asyncio.Event()
    resolve_calls = 0

    class Resolver:
        unavailable_reason = None

        async def resolve(self, artifact_id):
            nonlocal resolve_calls
            resolve_calls += 1
            if resolve_calls == 2:
                reauthorization_started.set()
                await reauthorization_gate.wait()
                raise PermissionError("stale authorization")
            return "az://account/container/blob.csv"

    class Fetcher:
        def can_handle(self, qualified_name):
            return qualified_name.startswith("az://")

        async def fetch_to_file(self, qualified_name, dest_path):
            dest_path.write_text("initial")
            return len("initial")

    manager = DataLakeDataManager(
        extra_fetchers=[cast(AssetFetcher, Fetcher())],
        artifact_resolver=cast(Any, Resolver()),
    )
    reference = "<blob>catalog-v1:opaque</blob>"
    await manager.get_cache_path(reference)
    stale = asyncio.create_task(manager.get_cache_path(reference))
    await reauthorization_started.wait()

    manager.invalidate_cache_entries(artifact_id_prefix="catalog-v1:")
    newer_path = manager._cache_dir / "newer.csv"
    newer_path.write_text("newer")
    manager._cache_index["catalog-v1:opaque"] = newer_path
    reauthorization_gate.set()

    assert await stale == newer_path
    assert manager._cache_index["catalog-v1:opaque"] == newer_path
    await manager.aclose()


async def test_local_catalog_shaped_id_is_not_catalog_generation_scoped(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = Path("catalog-v1:local-file")
    source.write_text("payload")

    class CatalogShapedLocalFetcher:
        def can_handle(self, qualified_name):
            return qualified_name == str(source)

        async def fetch_to_file(self, qualified_name, dest_path):
            dest_path.write_bytes(Path(qualified_name).read_bytes())
            return dest_path.stat().st_size

    manager = DataLakeDataManager(extra_fetchers=[cast(AssetFetcher, CatalogShapedLocalFetcher())])

    cached = await manager.get_cache_path(f"<local>{source}</local>")
    manager.invalidate_cache_entries(artifact_id_prefix="catalog-v1:")

    assert await manager.get_cache_path(f"<local>{source}</local>") == cached
    await manager.aclose()


async def test_invalidated_file_validation_does_not_remove_newer_cache_entry(monkeypatch):
    validation_started = asyncio.Event()
    validation_gate = asyncio.Event()
    hash_calls = 0

    class Resolver:
        unavailable_reason = None

        async def resolve(self, artifact_id):
            return "az://account/container/blob.csv"

    class Fetcher:
        def can_handle(self, qualified_name):
            return qualified_name.startswith("az://")

        async def fetch_to_file(self, qualified_name, dest_path):
            dest_path.write_text("initial")
            return len("initial")

    async def pause_first_hash(*args, **kwargs):
        nonlocal hash_calls
        del args, kwargs
        hash_calls += 1
        if hash_calls == 1:
            validation_started.set()
            await validation_gate.wait()
        return "0" * 64

    manager = DataLakeDataManager(
        extra_fetchers=[cast(AssetFetcher, Fetcher())],
        artifact_resolver=cast(Any, Resolver()),
    )
    reference = "<blob>catalog-v1:opaque</blob>"
    await manager.get_cache_path(reference)
    monkeypatch.setattr("agora_workbench.code_execution.data_access.manager.hash_file", pause_first_hash)
    stale = asyncio.create_task(
        manager.get_cache_path(reference, transfer_options=TransferOptions(expected_sha256="0" * 64))
    )
    await validation_started.wait()

    manager.invalidate_cache_entries(artifact_id_prefix="catalog-v1:")
    newer_path = manager._cache_dir / "newer.csv"
    newer_path.write_text("newer")
    manager._cache_index["catalog-v1:opaque"] = newer_path
    validation_gate.set()

    assert await stale == newer_path
    assert manager._cache_index["catalog-v1:opaque"] == newer_path
    await manager.aclose()


async def test_full_cache_invalidation_rejects_in_flight_non_catalog_fetch(tmp_path):
    started = asyncio.Event()
    gate = asyncio.Event()
    fetch_calls = 0
    source = tmp_path / "source.txt"
    source.write_text("payload")

    class Fetcher:
        def can_handle(self, qualified_name):
            return qualified_name == str(source)

        async def fetch_to_file(self, qualified_name, dest_path):
            nonlocal fetch_calls
            fetch_calls += 1
            if fetch_calls == 1:
                started.set()
                await gate.wait()
            dest_path.write_text(f"fetch-{fetch_calls}")
            return dest_path.stat().st_size

    manager = DataLakeDataManager(extra_fetchers=[cast(AssetFetcher, Fetcher())])
    fetch = asyncio.create_task(manager.get_cache_path(f"<local>{source}</local>"))
    await started.wait()

    manager.invalidate_cache_entries()
    gate.set()
    path = await fetch

    assert path.read_text() == "fetch-2"
    assert fetch_calls == 2
    await manager.aclose()


@pytest.mark.parametrize(
    "denial",
    [
        PermissionError("authorization revoked"),
        ArtifactNotFoundError("Artifact removed.", operation="resolve"),
    ],
)
async def test_catalog_cache_hits_are_reauthorized_and_evicted_on_denial(denial):
    class Resolver:
        unavailable_reason = None

        def __init__(self):
            self.denial = None

        async def resolve(self, artifact_id):
            if self.denial is not None:
                raise self.denial
            return "az://account/container/blob.csv"

    class Fetcher:
        def can_handle(self, qualified_name):
            return qualified_name.startswith("az://")

        async def fetch_to_file(self, qualified_name, dest_path):
            dest_path.write_text("blob-payload")
            return len("blob-payload")

    resolver = Resolver()
    manager = DataLakeDataManager(
        extra_fetchers=[cast(AssetFetcher, Fetcher())],
        artifact_resolver=cast(Any, resolver),
    )
    reference = "<blob>catalog-v1:opaque</blob>"
    cached_path = await manager.get_cache_path(reference)
    resolver.denial = denial

    with pytest.raises(type(denial)):
        await manager.get_cache_path(reference)

    assert "catalog-v1:opaque" not in manager._cache_index
    assert not cached_path.exists()
    await manager.aclose()


async def test_catalog_cache_transient_reauthorization_failure_preserves_cached_bytes():
    class Resolver:
        unavailable_reason = None
        fail = False

        async def resolve(self, artifact_id):
            if self.fail:
                raise BackendUnavailableError("Catalog unavailable.", operation="resolve")
            return "az://account/container/blob.csv"

    class Fetcher:
        def can_handle(self, qualified_name):
            return qualified_name.startswith("az://")

        async def fetch_to_file(self, qualified_name, dest_path):
            dest_path.write_text("blob-payload")
            return len("blob-payload")

    resolver = Resolver()
    manager = DataLakeDataManager(
        extra_fetchers=[cast(AssetFetcher, Fetcher())],
        artifact_resolver=cast(Any, resolver),
    )
    reference = "<blob>catalog-v1:opaque</blob>"
    cached_path = await manager.get_cache_path(reference)
    resolver.fail = True

    with pytest.raises(BackendUnavailableError):
        await manager.get_cache_path(reference)

    assert manager._cache_index["catalog-v1:opaque"] == cached_path
    assert cached_path.read_text() == "blob-payload"
    await manager.aclose()


async def test_session_credential_retries_cancelled_retired_provider_cleanup():
    class CredentialProvider:
        def __init__(self, *, cancel_once=False):
            self.cancel_once = cancel_once
            self.close_calls = 0

        async def close(self):
            self.close_calls += 1
            if self.cancel_once:
                self.cancel_once = False
                raise asyncio.CancelledError

    retired = CredentialProvider(cancel_once=True)
    current = CredentialProvider()
    credential = SessionCredential(retired, provider_factory=lambda token: current)
    credential.prepare_context_refresh(SessionContext("session", "user", "token"))()

    with pytest.raises(asyncio.CancelledError):
        await credential.close()
    await credential.close()

    assert retired.close_calls == 2
    assert current.close_calls == 1


async def test_cleanup_tracker_retries_only_pending_resources():
    class Resource:
        def __init__(self, *, cancel_once=False):
            self.cancel_once = cancel_once
            self.close_calls = 0

        async def close(self):
            self.close_calls += 1
            if self.cancel_once:
                self.cancel_once = False
                raise asyncio.CancelledError

    completed = Resource()
    cancelled = Resource(cancel_once=True)
    pending: list[object] = [completed, cancelled]
    tracker = _AsyncCleanupTracker()

    def cleanup():
        return _close_resources(pending)

    tracker.schedule(cleanup(), retry=cleanup)
    with pytest.raises(asyncio.CancelledError):
        await tracker.drain()

    assert completed.close_calls == 1
    assert cancelled.close_calls == 2
    assert pending == []


async def test_cleanup_tracker_discards_successful_background_cleanup():
    tracker = _AsyncCleanupTracker()
    finished = asyncio.Event()

    async def cleanup():
        finished.set()

    tracker.schedule(cleanup())
    await finished.wait()
    await asyncio.sleep(0)

    assert tracker._tasks == {}


def test_cleanup_tracker_retries_synchronous_cancellation_without_event_loop():
    tracker = _AsyncCleanupTracker()
    attempts = 0

    async def cleanup():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        tracker.schedule(cleanup(), retry=cleanup)

    assert attempts == 2


async def test_failed_context_refresh_closes_uncommitted_credential_provider():
    class CredentialProvider:
        def __init__(self, token):
            self.token = token
            self.close_calls = 0

        async def close(self):
            self.close_calls += 1

    providers = []

    def provider_factory(token):
        provider = CredentialProvider(token)
        providers.append(provider)
        return provider

    credential = SessionCredential(provider_factory("old-token"), provider_factory=provider_factory)
    integration = CatalogIntegration(
        ResourceLease(_LifecycleProvider()),
        authorizer=_PerUserAuthorizer("source"),
    )
    binding = integration.bind_session(SessionContext("session", "user", "old-token"), execution_references=True)
    binding.add_context_refresher(credential.prepare_context_refresh)

    def fail_refresh(context):
        del context
        raise ValueError("later refresh failed")

    binding.add_context_refresher(fail_refresh)

    with pytest.raises(ValueError, match="later refresh failed"):
        binding.refresh_context(SessionContext("session", "user", "new-token"))
    await integration._cleanup_tracker.drain()

    assert credential._provider is providers[0]
    assert providers[0].close_calls == 0
    assert providers[1].close_calls == 1

    await credential.close()
    await binding.aclose()


async def test_cancelled_context_refresh_closes_uncommitted_credential_provider():
    class CredentialProvider:
        def __init__(self, token):
            self.token = token
            self.close_calls = 0

        async def get_token(self, scope):
            del scope
            return self.token

        async def close(self):
            self.close_calls += 1

    providers = []

    def provider_factory(token):
        provider = CredentialProvider(token)
        providers.append(provider)
        return provider

    credential = SessionCredential(provider_factory("old-token"), provider_factory=provider_factory)
    integration = CatalogIntegration(
        ResourceLease(_LifecycleProvider()),
        authorizer=_PerUserAuthorizer("source"),
    )
    binding = integration.bind_session(SessionContext("session", "user", "old-token"), execution_references=True)
    binding.add_context_refresher(credential.prepare_context_refresh)

    def cancel_refresh(context):
        del context
        raise asyncio.CancelledError

    binding.add_context_refresher(cancel_refresh)

    with pytest.raises(asyncio.CancelledError):
        binding.refresh_context(SessionContext("session", "user", "new-token"))
    await integration._cleanup_tracker.drain()

    # The uncommitted replacement provider is closed exactly once, while the
    # prior committed provider is left untouched and still serves tokens.
    assert providers[1].close_calls == 1
    assert providers[0].close_calls == 0
    assert credential._provider is providers[0]
    assert await credential.get_token("scope") == "old-token"

    await credential.close()
    await binding.aclose()


async def test_context_refresh_rolls_back_already_committed_refreshers():
    integration = CatalogIntegration(
        ResourceLease(_LifecycleProvider()),
        authorizer=_PerUserAuthorizer("source"),
    )
    binding = integration.bind_session(SessionContext("session", "user", "old-token"), execution_references=True)
    state = {"value": "old"}

    def prepare_first(context):
        previous = state["value"]
        return _PreparedContextRefresh(
            lambda: state.__setitem__("value", context.user_token),
            rollback=lambda: state.__setitem__("value", previous),
        )

    def prepare_second(context):
        del context

        def fail():
            raise ValueError("commit failed")

        return _PreparedContextRefresh(fail)

    binding.add_context_refresher(prepare_first)
    binding.add_context_refresher(prepare_second)
    previous_context = binding.context

    with pytest.raises(ValueError, match="commit failed"):
        binding.refresh_context(SessionContext("session", "user", "new-token"))

    assert state["value"] == "old"
    assert binding.context is previous_context
    await binding.aclose()


async def test_context_refresh_rolls_back_refresher_that_mutates_then_raises():
    integration = CatalogIntegration(
        ResourceLease(_LifecycleProvider()),
        authorizer=_PerUserAuthorizer("source"),
    )
    binding = integration.bind_session(SessionContext("session", "user", "old-token"), execution_references=True)
    state = {"value": "old"}

    def prepare(context):
        previous = state["value"]

        def commit():
            state["value"] = context.user_token
            raise RuntimeError("commit failed")

        return _PreparedContextRefresh(commit, rollback=lambda: state.__setitem__("value", previous))

    binding.add_context_refresher(prepare)

    with pytest.raises(RuntimeError, match="commit failed"):
        binding.refresh_context(SessionContext("session", "user", "new-token"))

    assert state["value"] == "old"
    await binding.aclose()


async def test_context_refresh_rebuilds_and_retires_capability_extensions():
    extensions = []

    class Extension:
        def __init__(self, token):
            self.token = token
            self.close_calls = 0

        async def aclose(self):
            self.close_calls += 1

    def extension_factory(context, catalog, request_context):
        del catalog, request_context
        extension = Extension(context.user_token)
        extensions.append(extension)
        return extension

    integration = CatalogIntegration(
        ResourceLease(_LifecycleProvider()),
        authorizer=_PerUserAuthorizer("source"),
        capability_extension_factory=extension_factory,
    )
    binding = integration.bind_session(SessionContext("session", "user", "old-token"), execution_references=True)

    binding.refresh_context(SessionContext("session", "user", "new-token"))
    await integration._cleanup_tracker.drain()

    assert [extension.token for extension in extensions] == ["old-token", "new-token"]
    assert binding.capability_extensions == (extensions[1],)
    assert extensions[0].close_calls == 1
    assert extensions[1].close_calls == 0

    await binding.aclose()
    assert extensions[1].close_calls == 1


async def test_effective_capabilities_reuse_authorized_read_snapshot():
    integration = CatalogIntegration(
        ResourceLease(_LifecycleProvider()),
        authorizer=_PerUserAuthorizer("source"),
    )
    binding = integration.bind_session(SessionContext("session", "user", "token"), execution_references=True)
    binding.catalog.capabilities = AsyncMock(side_effect=AssertionError("capabilities fetched twice"))
    read_capabilities = (SourceCapabilities("source", frozenset({CatalogOperation.SEARCH})),)

    capabilities = await integration.capabilities(binding, read_capabilities)

    assert capabilities == read_capabilities
    binding.catalog.capabilities.assert_not_awaited()
    await binding.aclose()


async def test_context_refresh_extension_failure_closes_new_authorizer():
    authorizers = []

    class Authorizer:
        def __init__(self):
            self.close_calls = 0

        async def authorize(self, request, context):
            return True

        async def aclose(self):
            self.close_calls += 1

    def authorizer_factory(context):
        del context
        authorizer = Authorizer()
        authorizers.append(authorizer)
        return authorizer

    integration = CatalogIntegration(
        ResourceLease(_LifecycleProvider()),
        authorizer_factory=authorizer_factory,
        capability_extension_factory=lambda context, catalog, request_context: (
            (_ for _ in ()).throw(RuntimeError("extension refresh failed"))
            if context.user_token == "new-token"
            else None
        ),
    )
    binding = integration.bind_session(SessionContext("session", "user", "old-token"), execution_references=True)

    with pytest.raises(RuntimeError, match="extension refresh failed"):
        binding.refresh_context(SessionContext("session", "user", "new-token"))
    await integration._cleanup_tracker.drain()

    assert binding.owned_authorizer is authorizers[0]
    assert authorizers[0].close_calls == 0
    assert authorizers[1].close_calls == 1
    await binding.aclose()


async def test_context_refresh_catalog_construction_failure_closes_new_authorizer(monkeypatch):
    authorizers = []

    class Authorizer:
        def __init__(self):
            self.close_calls = 0

        async def authorize(self, request, context):
            return True

        async def aclose(self):
            self.close_calls += 1

    def authorizer_factory(context):
        del context
        authorizer = Authorizer()
        authorizers.append(authorizer)
        return authorizer

    integration = CatalogIntegration(
        ResourceLease(_LifecycleProvider()),
        authorizer_factory=authorizer_factory,
    )
    binding = integration.bind_session(SessionContext("session", "user", "old-token"), execution_references=True)

    def fail_catalog(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("catalog construction failed")

    monkeypatch.setattr("agora_workbench.code_execution.catalog_integration.AuthorizedCatalogProvider", fail_catalog)
    with pytest.raises(RuntimeError, match="catalog construction failed"):
        binding.refresh_context(SessionContext("session", "user", "new-token"))
    await integration._cleanup_tracker.drain()

    assert binding.owned_authorizer is authorizers[0]
    assert authorizers[0].close_calls == 0
    assert authorizers[1].close_calls == 1
    await binding.aclose()


async def test_concurrent_catalog_binding_close_coalesces_resource_cleanup():
    started = asyncio.Event()
    gate = asyncio.Event()
    close_calls = 0

    class Extension:
        async def aclose(self):
            nonlocal close_calls
            close_calls += 1
            started.set()
            await gate.wait()

    integration = CatalogIntegration(
        ResourceLease(_LifecycleProvider()),
        authorizer=_PerUserAuthorizer("source"),
    )
    binding = integration.bind_session(SessionContext("session", "user", "token"), execution_references=True)
    binding.capability_extensions = (Extension(),)

    first = asyncio.create_task(binding.aclose())
    await started.wait()
    second = asyncio.create_task(binding.aclose())
    await asyncio.sleep(0)
    gate.set()
    await asyncio.gather(first, second)

    assert close_calls == 1


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


async def test_data_manager_construction_failure_closes_session_credential(tmp_path, monkeypatch):
    credentials = []

    class CredentialProvider:
        def __init__(self):
            self.close_calls = 0

        async def close(self):
            self.close_calls += 1

    def credential_factory(token):
        del token
        credential = CredentialProvider()
        credentials.append(credential)
        return credential

    class FailingManager:
        def __init__(self, *args, **kwargs):
            del args, kwargs
            raise RuntimeError("manager construction failed")

    auth = create_noop_auth_config()
    auth.credential_provider_factory = credential_factory
    integration = CatalogIntegration(
        ResourceLease(_LifecycleProvider()),
        authorizer=_PerUserAuthorizer("source"),
    )
    monkeypatch.setattr("agora_workbench.code_execution.data_access.manager.DataLakeDataManager", FailingManager)
    server = CodeExecutionServer(_server_config(tmp_path), auth_config=auth, catalog=integration)

    with pytest.raises(RuntimeError, match="manager construction failed"):
        server.session_manager.create_session({}, "user", "token", {})
    await integration._cleanup_tracker.drain()

    assert credentials[0].close_calls == 1
    await integration.shutdown()


async def test_catalog_extension_collision_rolls_back_custom_session_resources(tmp_path):
    class Extension:
        def __init__(self):
            self.cleanup_calls = 0

        def cleanup(self):
            self.cleanup_calls += 1

    extension = Extension()
    manager = DataLakeDataManager()
    cache_dir = manager._cache_dir
    session_manager = SessionManager(
        SessionConfig(
            data_manager_factory=lambda context: SessionResources(
                manager,
                {"catalog": extension},
            )
        )
    )
    integration = CatalogIntegration(
        ResourceLease(_LifecycleProvider()),
        authorizer=_PerUserAuthorizer("source"),
    )
    CodeExecutionServer(
        _server_config(tmp_path),
        auth_config=create_noop_auth_config(),
        session_manager=session_manager,
        catalog=integration,
    )

    with pytest.raises(ValueError, match="reserved 'catalog' key"):
        session_manager.create_session({}, "user", "token", {})
    await session_manager.await_resource_cleanup()
    await integration.shutdown()

    assert extension.cleanup_calls == 1
    assert not cache_dir.exists()


async def test_invalid_factory_manager_rolls_back_extensions():
    class Extension:
        def __init__(self):
            self.cleanup_calls = 0

        def cleanup(self):
            self.cleanup_calls += 1

    extension = Extension()
    session_manager = SessionManager(
        SessionConfig(
            data_manager_factory=lambda context: SessionResources(
                cast(Any, object()),
                {"extension": extension},
            )
        )
    )

    with pytest.raises(TypeError, match="cleanup\\(\\) method"):
        session_manager.create_session({}, "user", "token", {})
    await session_manager.await_resource_cleanup()

    assert extension.cleanup_calls == 1


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


async def test_owned_catalog_provider_supports_cleanup_lifecycle():
    class Provider:
        def __init__(self):
            self.cleanup_calls = 0

        def cleanup(self):
            self.cleanup_calls += 1

    provider = Provider()
    integration = CatalogIntegration(
        ResourceLease(cast(Any, provider), ResourceOwnership.OWNED),
        authorizer=_PerUserAuthorizer("source"),
        load_on_startup=False,
    )

    await integration.shutdown()
    await integration.shutdown()

    assert provider.cleanup_calls == 1


async def test_owned_provider_closes_once_across_concurrent_and_repeated_shutdown():
    close_started = asyncio.Event()
    close_gate = asyncio.Event()

    class Provider(_LifecycleProvider):
        async def aclose(self):
            self.close_calls += 1
            if self.close_calls > 1:
                raise RuntimeError("provider closed twice")
            close_started.set()
            await close_gate.wait()

    provider = Provider()
    integration = CatalogIntegration(
        ResourceLease(provider, ResourceOwnership.OWNED),
        authorizer=_PerUserAuthorizer("source"),
        load_on_startup=False,
    )
    first = asyncio.create_task(integration.shutdown())
    second = asyncio.create_task(integration.shutdown())
    await close_started.wait()
    assert provider.close_calls == 1
    close_gate.set()
    await asyncio.gather(first, second)

    await integration.shutdown()
    assert provider.close_calls == 1


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


async def test_validated_refresh_claims_replace_stale_catalog_claims(tmp_path):
    integration = CatalogIntegration(
        ResourceLease(_LifecycleProvider()),
        authorizer_factory=lambda context: _PerUserAuthorizer("source"),
    )
    server = CodeExecutionServer(
        _server_config(tmp_path),
        auth_config=create_noop_auth_config(),
        catalog=integration,
    )
    session_id = server.session_manager.create_session(
        {},
        user_identity="user@tenant",
        user_token="old-token",
        token_claims={"role": "reader"},
    )
    server.validate_token = AsyncMock(return_value={"oid": "user", "tid": "tenant", "role": "writer"})

    set_current_user_identity("user@tenant")
    set_current_request_token("new-token")
    set_current_token_claims(None)
    try:
        session = await server._get_or_create_session("search_data", session_id=session_id)
    finally:
        set_current_user_identity(None)
        set_current_request_token(None)
        set_current_token_claims(None)

    assert session.token_claims["role"] == "writer"
    assert session.extensions["catalog"].context.attributes["claims"]["role"] == "writer"
    await server.session_manager.aclose_all_sessions()
    await integration.shutdown()


async def test_refresh_retains_authorizer_until_request_snapshot_releases():
    authorizers = []

    class Authorizer:
        def __init__(self):
            self.close_calls = 0
            authorizers.append(self)

        async def authorize(self, request, context):
            return True

        async def aclose(self):
            self.close_calls += 1

    integration = CatalogIntegration(
        ResourceLease(_LifecycleProvider()),
        authorizer_factory=lambda context: Authorizer(),
    )
    binding = integration.bind_session(SessionContext("session", "user", "old"), execution_references=True)
    snapshot = binding.snapshot()

    binding.refresh_context(SessionContext("session", "user", "new"))
    await asyncio.sleep(0)
    assert authorizers[0].close_calls == 0

    snapshot.close()
    await integration._cleanup_tracker.drain()
    assert authorizers[0].close_calls == 1

    await binding.aclose()


async def test_refresh_reactivates_deferred_authorizer_without_closing_it():
    class Authorizer:
        def __init__(self, name):
            self.name = name
            self.close_calls = 0

        async def authorize(self, request, context):
            return True

        async def aclose(self):
            self.close_calls += 1

    first = Authorizer("first")
    second = Authorizer("second")
    by_token = {"first": first, "second": second, "first-again": first}
    integration = CatalogIntegration(
        ResourceLease(_LifecycleProvider()),
        authorizer_factory=lambda context: by_token[context.user_token],
    )
    binding = integration.bind_session(SessionContext("session", "user", "first"), execution_references=True)
    snapshot = binding.snapshot()

    binding.refresh_context(SessionContext("session", "user", "second"))
    binding.refresh_context(SessionContext("session", "user", "first-again"))
    snapshot.close()
    await integration._cleanup_tracker.drain()

    assert binding.owned_authorizer is first
    assert first.close_calls == 0
    assert second.close_calls == 1
    assert await binding.catalog.capabilities(binding.context)
    await binding.aclose()
    assert first.close_calls == 1


async def test_resolver_retains_authorizer_until_resolution_finishes():
    authorize_started = asyncio.Event()
    authorize_gate = asyncio.Event()
    authorizers = []

    class Authorizer:
        def __init__(self):
            self.close_calls = 0
            authorizers.append(self)

        async def authorize(self, request, context):
            if self is authorizers[0]:
                authorize_started.set()
                await authorize_gate.wait()
            return True

        async def aclose(self):
            self.close_calls += 1

    class Provider(_LifecycleProvider):
        async def resolve(self, reference, context):
            return ResolvedArtifact(reference, StorageLocator("https://example.invalid/artifact"))

    integration = CatalogIntegration(
        ResourceLease(Provider()),
        authorizer_factory=lambda context: Authorizer(),
    )
    binding = integration.bind_session(SessionContext("session", "user", "old"), execution_references=True)
    resolving = asyncio.create_task(
        binding.resolver.resolve(_encode_reference(ArtifactReference("artifact", "source")))
    )
    await authorize_started.wait()

    binding.refresh_context(SessionContext("session", "user", "new"))
    await asyncio.sleep(0)
    assert authorizers[0].close_calls == 0

    authorize_gate.set()
    assert await resolving == "https://example.invalid/artifact"
    await integration._cleanup_tracker.drain()
    assert authorizers[0].close_calls == 1
    await binding.aclose()


async def test_closing_binding_rejects_new_request_snapshots():
    integration = CatalogIntegration(
        ResourceLease(_LifecycleProvider()),
        authorizer=_PerUserAuthorizer("source"),
    )
    binding = integration.bind_session(SessionContext("session", "user", "token"), execution_references=True)
    snapshot = binding.snapshot()
    closing = asyncio.create_task(binding.aclose())
    await asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="closed"):
        binding.snapshot()
    with pytest.raises(RuntimeError, match="closed"):
        binding.refresh_context(SessionContext("session", "user", "new-token"))

    snapshot.close()
    _ = await closing


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


async def test_cancelled_catalog_startup_awaits_owned_provider_close():
    load_started = asyncio.Event()
    close_started = asyncio.Event()
    close_gate = asyncio.Event()

    class Provider(_LifecycleProvider):
        async def load(self):
            load_started.set()
            await asyncio.Event().wait()

        async def aclose(self):
            self.close_calls += 1
            close_started.set()
            await close_gate.wait()

    provider = Provider()
    integration = CatalogIntegration(
        ResourceLease(provider, ResourceOwnership.OWNED),
        authorizer=_PerUserAuthorizer("source"),
    )
    startup = asyncio.create_task(integration.startup())
    await load_started.wait()
    startup.cancel()
    await close_started.wait()
    await asyncio.sleep(0)
    assert not startup.done()

    close_gate.set()
    with pytest.raises(asyncio.CancelledError):
        _ = await startup
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
            "name": "metadata-name",
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

    async def effective_capabilities(current, read_capabilities=None):
        return read_capabilities if read_capabilities is not None else await current.catalog.capabilities()

    integration = SimpleNamespace(
        capabilities=AsyncMock(side_effect=effective_capabilities),
        _policy_mode=CatalogPolicyMode.HOMOGENEOUS_SOURCE,
    )
    register_catalog_discovery_tools(fake_server, cast(CatalogIntegration, integration))

    result = await captured["search_data"]("data")
    assert result[0]["id"] == "artifact"
    assert result[0]["name"] == "data.csv"
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
    assert catalog.search.await_args.args[0].source_ids == ("source",)
    details = await captured["get_artifact"]("artifact")
    assert details["current_revision"] == 2
    assert await captured["list_domains"]() == ["science https://example.test/domain"]
    assert catalog.list.await_args.args[0].source_ids == ("source",)

    integration._policy_mode = CatalogPolicyMode.PER_ARTIFACT
    per_artifact = await captured["search_data"]("data")
    assert "load_path" not in per_artifact[0]
    per_artifact_capabilities = await captured["get_catalog_capabilities"]()
    assert not per_artifact_capabilities["execution_references"]

    catalog.capabilities.return_value = (SourceCapabilities("source", frozenset({CatalogOperation.SEARCH})),)
    integration.capabilities.return_value = (
        SourceCapabilities("source", frozenset({CatalogOperation.SEARCH, CatalogOperation.RESOLVE})),
    )
    capabilities = await captured["get_catalog_capabilities"]()
    assert not capabilities["execution_references"]


async def test_catalog_discovery_restores_transport_auth_before_session_lookup():
    auth_state = {"identity": "stale-user"}
    binding = SimpleNamespace(
        catalog=SimpleNamespace(
            search=AsyncMock(return_value=Page(())),
            capabilities=AsyncMock(return_value=()),
        ),
        context=RequestContext(),
        execution_references=False,
    )
    session = SimpleNamespace(data_manager=SimpleNamespace(), extensions={"catalog": binding})
    captured = {}

    def restore_auth(session_id):
        assert session_id == "transport-session"
        auth_state["identity"] = "session-user"

    async def get_session(tool_name, *, session_id):
        assert tool_name == "search_data"
        assert session_id == "transport-session"
        assert auth_state["identity"] == "session-user"
        return session

    server = SimpleNamespace(
        mcp=SimpleNamespace(tool=lambda name, description: lambda function: captured.setdefault(name, function)),
        _restore_auth_context_for_mcp_session=restore_auth,
        _get_or_create_session=get_session,
    )
    integration = SimpleNamespace(
        capabilities=AsyncMock(),
        _policy_mode=CatalogPolicyMode.HOMOGENEOUS_SOURCE,
    )
    register_catalog_discovery_tools(server, cast(CatalogIntegration, integration))

    assert await captured["search_data"]("data", mcp_ctx=SimpleNamespace(session_id="transport-session")) == []


async def test_application_capability_adapter_holds_binding_snapshot():
    closed = False

    class Snapshot:
        def close(self):
            nonlocal closed
            closed = True

    snapshot = Snapshot()
    binding = SimpleNamespace(snapshot=lambda: snapshot)
    catalog = SimpleNamespace(capabilities=AsyncMock(return_value=("capability",)))
    server = SimpleNamespace(catalog=catalog)
    session = SimpleNamespace(extensions={"catalog": binding})

    assert await CodeExecutionServer.get_data_lake_capabilities(cast(Any, server), cast(Any, session)) == (
        "capability",
    )
    catalog.capabilities.assert_awaited_once_with(snapshot)
    assert closed


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


async def test_discovery_request_keeps_immutable_authorization_snapshot():
    started = asyncio.Event()
    gate = asyncio.Event()
    artifact = CatalogArtifact(ArtifactReference("artifact", "source"), ArtifactPresentation("data.csv"))

    async def old_capabilities(context):
        assert context == "reader-context"
        started.set()
        await gate.wait()
        return (SourceCapabilities("source", frozenset({CatalogOperation.GET})),)

    old_catalog = SimpleNamespace(
        capabilities=AsyncMock(side_effect=old_capabilities),
        get=AsyncMock(return_value=artifact),
    )
    new_catalog = SimpleNamespace(
        capabilities=AsyncMock(return_value=(SourceCapabilities("source", frozenset({CatalogOperation.GET})),)),
        get=AsyncMock(return_value=artifact),
    )
    binding = SimpleNamespace(
        catalog=old_catalog,
        context="reader-context",
        execution_references=False,
        capability_extensions=(),
    )
    captured = {}
    server = SimpleNamespace(
        mcp=SimpleNamespace(tool=lambda name, description: lambda function: captured.setdefault(name, function)),
        _get_or_create_session=AsyncMock(
            return_value=SimpleNamespace(data_manager=SimpleNamespace(), extensions={"catalog": binding})
        ),
    )
    integration = SimpleNamespace(
        capabilities=AsyncMock(),
        _policy_mode=CatalogPolicyMode.HOMOGENEOUS_SOURCE,
    )
    register_catalog_discovery_tools(server, cast(CatalogIntegration, integration))

    request = asyncio.create_task(captured["get_artifact"]("artifact", source_id="source"))
    await started.wait()
    binding.catalog = new_catalog
    binding.context = "writer-context"
    gate.set()
    assert (await request)["id"] == "artifact"

    old_catalog.get.assert_awaited_once()
    assert old_catalog.get.await_args.args[1] == "reader-context"
    new_catalog.get.assert_not_awaited()


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
    cancelled_attempts = 0
    resolver_close_calls = 0

    class CancelledExtension:
        async def aclose(self):
            nonlocal cancelled_attempts
            cancelled_attempts += 1
            if cancelled_attempts == 1:
                raise asyncio.CancelledError()

    class LaterExtension:
        def __init__(self):
            self.close_calls = 0

        async def aclose(self):
            self.close_calls += 1
            if self.close_calls > 1:
                raise RuntimeError("extension closed twice")

    later_extension = LaterExtension()
    provider = _LifecycleProvider()
    integration = CatalogIntegration(
        ResourceLease(provider, ResourceOwnership.OWNED),
        authorizer=_PerUserAuthorizer("source"),
        capability_extension_factory=lambda context, catalog, request_context: (
            CancelledExtension(),
            later_extension,
        ),
    )
    binding = integration.bind_session(SessionContext("session", "user", "token"), execution_references=True)

    async def close_resolver():
        nonlocal resolver_close_calls
        resolver_close_calls += 1
        if resolver_close_calls > 1:
            raise RuntimeError("resolver closed twice")

    binding.resolver.aclose = close_resolver

    with pytest.raises(asyncio.CancelledError):
        await binding.aclose()

    assert later_extension.close_calls == 1
    assert binding._closed
    with pytest.raises(RuntimeError, match="closed"):
        binding.snapshot()
    await binding.aclose()
    assert cancelled_attempts == 2
    assert resolver_close_calls == 1
    assert later_extension.close_calls == 1
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
    _ = await drain


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
    _ = await shutdown


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


async def test_cancelled_catalog_shutdown_waits_for_cleanup_before_provider():
    started = asyncio.Event()
    gate = asyncio.Event()
    finished = asyncio.Event()
    provider = _LifecycleProvider()

    class Extension:
        async def aclose(self):
            started.set()
            await gate.wait()
            finished.set()

    integration = CatalogIntegration(
        ResourceLease(provider, ResourceOwnership.OWNED),
        authorizer=_PerUserAuthorizer("source"),
        capability_extension_factory=lambda context, catalog, request_context: Extension(),
    )
    binding = integration.bind_session(SessionContext("session", "user", "token"), execution_references=True)
    binding.cleanup()
    await started.wait()

    first_shutdown = asyncio.create_task(integration.shutdown())
    await asyncio.sleep(0)
    first_shutdown.cancel()
    await asyncio.sleep(0)
    assert not first_shutdown.done()
    assert provider.close_calls == 0
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        _ = await first_shutdown
    assert finished.is_set()
    assert provider.close_calls == 1

    await integration.shutdown()


async def test_cancelled_catalog_drain_remains_tracked_for_next_drain():
    started = asyncio.Event()
    gate = asyncio.Event()

    class Extension:
        async def aclose(self):
            started.set()
            await gate.wait()

    integration = CatalogIntegration(
        ResourceLease(_LifecycleProvider()),
        authorizer=_PerUserAuthorizer("source"),
        capability_extension_factory=lambda context, catalog, request_context: Extension(),
    )
    binding = integration.bind_session(SessionContext("session", "user", "token"), execution_references=True)
    binding.cleanup()
    await started.wait()

    first_drain = asyncio.create_task(integration._cleanup_tracker.drain())
    await asyncio.sleep(0)
    first_drain.cancel()
    with pytest.raises(asyncio.CancelledError):
        _ = await first_drain

    second_drain = asyncio.create_task(integration._cleanup_tracker.drain())
    await asyncio.sleep(0)
    assert not second_drain.done()
    gate.set()
    _ = await second_drain


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


async def test_cancelled_owned_provider_close_retries_before_propagating():
    class Provider(_LifecycleProvider):
        async def aclose(self):
            self.close_calls += 1
            if self.close_calls == 1:
                raise asyncio.CancelledError

    provider = Provider()
    integration = CatalogIntegration(
        ResourceLease(provider, ResourceOwnership.OWNED),
        authorizer=_PerUserAuthorizer("source"),
    )

    with pytest.raises(asyncio.CancelledError):
        await integration.shutdown()

    assert provider.close_calls == 2
    assert integration._provider_closed


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
