"""Offline v0.3.0 data-lake release acceptance tests."""

from __future__ import annotations

import asyncio
import importlib
import json
import shutil
import statistics
import subprocess
import sys
import time
import tracemalloc
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from agora_workbench.code_execution import CatalogIntegration, CodeExecutionServer, ServerConfig
from agora_workbench.code_execution.auth import create_noop_auth_config
from agora_workbench.code_execution.data_access import manager as manager_module
from agora_workbench.data_lake import (
    ArtifactMetadata,
    AuthorizedManagedCatalogWriter,
    CatalogAuthorizationRequest,
    CatalogOperation,
    LocalManagedStorage,
    ListRequest,
    ManagedCatalogWriter,
    PermissionDeniedError,
    RequestContext,
    UploadArtifactRequest,
    managed_writer_extension_factory,
)
from agora_workbench.data_lake.catalog import (
    SCHEMA_VERSION,
    CatalogConfig,
    CatalogDB,
    DiscoveryMode,
    ManifestCatalogProvider,
    SourceConfig,
    convert_catalog_config,
)

FIXTURES = Path(__file__).parent / "fixtures" / "v0_2"


class _PrincipalAuthorizer:
    def __init__(self) -> None:
        self.read_sources = {
            "alice@tenant": {"managed"},
            "bob@tenant": {"restricted"},
        }

    async def authorize(self, request: CatalogAuthorizationRequest, context: RequestContext) -> bool:
        caller = context.caller_id or ""
        if request.operation in {
            CatalogOperation.SEARCH,
            CatalogOperation.LIST,
            CatalogOperation.GET,
            CatalogOperation.RESOLVE,
        }:
            return request.source_id in self.read_sources.get(caller, set())
        return caller == "alice@tenant" and request.source_id == "managed"


def _server_config(tmp_path: Path) -> ServerConfig:
    return ServerConfig(
        name="release-acceptance",
        description="Data-lake release acceptance",
        type="uv",
        dependency_file="[project]\nname='release-acceptance'\nversion='0.0.0'\n",
        build_dir=tmp_path / "environment",
        auto_build=False,
    )


def _manifest_source(root: Path, source_id: str) -> SourceConfig:
    return SourceConfig(
        source_id=source_id,
        path=str(root),
        discovery=DiscoveryMode.MANIFEST,
        manifest=".agora/manifest.json",
    )


def _write_read_only_source(root: Path) -> None:
    (root / ".agora").mkdir(parents=True)
    (root / "approved").mkdir()
    (root / "approved" / "forecast.csv").write_text("day,value\nmonday,private\n", encoding="utf-8")
    (root / ".agora" / "manifest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "generation": 1,
                "artifacts": [
                    {
                        "path": "approved/forecast.csv",
                        "artifact_id": "restricted-forecast",
                        "name": "restricted forecast",
                        "domain": "finance",
                        "media_type": "text/csv",
                        "aliases": ["external:shared"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


async def _call_tool(server: CodeExecutionServer, name: str, session_id: str, **kwargs: object) -> Any:
    tool = await server.mcp.get_tool(name)
    return await tool.fn(mcp_ctx=SimpleNamespace(session_id=session_id), **kwargs)


@pytest.mark.integration
async def test_local_public_surfaces_roundtrip_isolate_and_converge(tmp_path, monkeypatch):
    """Exercise managed writes through catalog, MCP, resolution, and transfer."""
    managed_root = tmp_path / "managed"
    restricted_root = tmp_path / "restricted"
    managed_root.mkdir()
    restricted_root.mkdir()
    _write_read_only_source(restricted_root)

    first_payload = tmp_path / "managed-forecast.csv"
    first_payload.write_text("day,value\nmonday,public\n", encoding="utf-8")
    storage = LocalManagedStorage(managed_root)
    writer = ManagedCatalogWriter("managed", storage)
    authorizer = _PrincipalAuthorizer()
    authorized_writer = AuthorizedManagedCatalogWriter(writer, authorizer)
    alice = RequestContext(request_id="write-1", caller_id="alice@tenant")
    bob = RequestContext(request_id="write-denied", caller_id="bob@tenant")
    first = await authorized_writer.upload(
        UploadArtifactRequest(
            operation_id="initial",
            path="forecast.csv",
            local_path=first_payload,
            artifact_id="managed-forecast",
            metadata=ArtifactMetadata(
                name="managed forecast",
                domain="weather",
                media_type="text/csv",
                aliases=("external:shared",),
            ),
        ),
        alice,
    )
    with pytest.raises(PermissionDeniedError):
        await authorized_writer.upload(
            UploadArtifactRequest("denied", "denied.csv", first_payload),
            bob,
        )

    cache_root = tmp_path / "session-caches"
    cache_root.mkdir()
    cache_counter = 0

    def isolated_cache(*, prefix: str) -> str:
        nonlocal cache_counter
        cache_counter += 1
        path = cache_root / f"{prefix}{cache_counter}"
        path.mkdir()
        return str(path)

    monkeypatch.setattr(manager_module.tempfile, "mkdtemp", isolated_cache)

    config = CatalogConfig(
        sources=[
            _manifest_source(managed_root, "managed"),
            _manifest_source(restricted_root, "restricted"),
        ]
    )
    integration = CatalogIntegration.from_config(
        config,
        authorizer=authorizer,
        capability_extension_factory=managed_writer_extension_factory(writer),
        db_path=tmp_path / "server-catalog.db",
    )
    server = CodeExecutionServer(
        _server_config(tmp_path),
        auth_config=create_noop_auth_config(),
        catalog=integration,
    )
    session_manager = server.session_manager
    await integration.startup()
    expiration = int(time.time()) + 3600
    alice_session = session_manager.create_session(
        {},
        "alice@tenant",
        "token-a",
        {"oid": "alice", "tid": "tenant", "exp": expiration},
    )
    bob_session = session_manager.create_session(
        {},
        "bob@tenant",
        "token-b",
        {"oid": "bob", "tid": "tenant", "exp": expiration},
    )

    second_reader = ManifestCatalogProvider(
        CatalogConfig(sources=[_manifest_source(managed_root, "managed")]),
        db_path=tmp_path / "reader-catalog.db",
    )
    await second_reader.load()
    try:
        alice_hits = await _call_tool(
            server,
            "search_data",
            alice_session,
            query="forecast",
            domain="weather",
            top=1,
        )
        bob_hits = await _call_tool(
            server,
            "search_data",
            bob_session,
            query="forecast",
            domain="finance",
            top=1,
        )
        assert [hit["id"] for hit in alice_hits] == ["managed-forecast"]
        assert [hit["id"] for hit in bob_hits] == ["restricted-forecast"]
        assert "storage_uri" not in alice_hits[0]
        assert "storage_uri" not in bob_hits[0]
        assert (await _call_tool(server, "list_domains", alice_session)) == ["weather"]
        assert (await _call_tool(server, "list_domains", bob_session)) == ["finance"]

        alice_alias = await _call_tool(
            server,
            "get_artifact",
            alice_session,
            artifact_id="external:shared",
            source_id="managed",
        )
        bob_alias = await _call_tool(
            server,
            "get_artifact",
            bob_session,
            artifact_id="external:shared",
            source_id="restricted",
        )
        assert alice_alias["id"] == "external:shared"
        assert bob_alias["id"] == "external:shared"
        assert (
            await _call_tool(
                server,
                "get_artifact",
                bob_session,
                artifact_id="managed-forecast",
                source_id="managed",
            )
        )["error_type"] == "not_found"

        tools = {tool.name for tool in await server.mcp.list_tools()}
        assert "query_catalog" not in tools
        assert not any("facet" in name for name in tools)

        load_path = alice_hits[0]["load_path"]
        cached = await session_manager.get_session(alice_session).data_manager.get_cache_path(load_path)
        assert cached.read_bytes() == first_payload.read_bytes()

        alice_capabilities = await _call_tool(server, "get_catalog_capabilities", alice_session)
        bob_capabilities = await _call_tool(server, "get_catalog_capabilities", bob_session)
        alice_operations = {
            operation
            for source in alice_capabilities["sources"]
            if source["source_id"] == "managed"
            for operation in source["operations"]
        }
        assert {"register", "upload", "promote", "remove"} <= alice_operations
        assert all(
            not ({"register", "upload", "promote", "remove"} & set(source["operations"]))
            for source in bob_capabilities["sources"]
        )

        concurrent_payloads = []
        for index in range(2):
            path = tmp_path / f"concurrent-{index}.txt"
            path.write_text(f"concurrent-{index}", encoding="utf-8")
            concurrent_payloads.append(path)
        results = await asyncio.gather(
            *[
                authorized_writer.upload(
                    UploadArtifactRequest(
                        operation_id=f"concurrent-{index}",
                        path=f"concurrent-{index}.txt",
                        local_path=path,
                    ),
                    alice,
                )
                for index, path in enumerate(concurrent_payloads)
            ]
        )
        assert {result.generation for result in results} == {first.generation + 1, first.generation + 2}

        await cast(Any, integration.provider).load()
        await second_reader.load()
        reader_generation = second_reader.readiness().sources[0].manifest_generation
        assert reader_generation == max(result.generation for result in results)
        assert len((await second_reader.list(ListRequest(), RequestContext())).items) == 3
        refreshed_hits = await _call_tool(
            server,
            "search_data",
            alice_session,
            query="concurrent",
            top=10,
        )
        assert {hit["name"] for hit in refreshed_hits} == {"concurrent-0.txt", "concurrent-1.txt"}

        def interrupt(step: str, operation_id: str) -> None:
            if step == "after_object" and operation_id == "abandoned":
                raise RuntimeError("simulated interruption")

        interrupted = ManagedCatalogWriter(
            "managed",
            LocalManagedStorage(managed_root),
            interruption_hook=interrupt,
        )
        orphan_payload = tmp_path / "orphan.txt"
        orphan_payload.write_text("orphan", encoding="utf-8")
        with pytest.raises(RuntimeError, match="simulated interruption"):
            await interrupted.upload(
                UploadArtifactRequest("abandoned", "orphan.txt", orphan_payload),
            )
        report = await writer.reconcile(grace_seconds=0)
        assert report.removed_orphans == ("abandoned",)
        assert not list((managed_root / ".agora" / "revisions").rglob("abandoned.data"))
    finally:
        await second_reader.aclose()
        await session_manager.aclose_all_sessions()
        await integration.shutdown()

    assert not any(cache_root.iterdir())
    with pytest.raises(ValueError, match="Write operations are not permitted"):
        db = CatalogDB(tmp_path / "server-catalog.db")
        db.open()
        try:
            db.execute_readonly("INSERT INTO artifacts(id) VALUES ('unsafe')")
        finally:
            db.close()


@pytest.mark.integration
def test_v0_2_fixtures_upgrade_mutate_export_and_compatibility(tmp_path, monkeypatch):
    import_contract = json.loads((FIXTURES / "import-paths.json").read_text(encoding="utf-8"))
    for module_name, symbols in import_contract.items():
        module = importlib.import_module(module_name)
        assert all(hasattr(module, symbol) for symbol in symbols)

    monkeypatch.chdir(FIXTURES)
    converted_path = tmp_path / "catalog-v1.yaml"
    report = convert_catalog_config(FIXTURES / "catalog.yaml", converted_path, dry_run=False)
    assert report.written
    assert CatalogConfig.from_yaml(converted_path).version == 1

    database = tmp_path / "catalog-v0.sqlite"
    shutil.copy2(FIXTURES / "catalog-v0.sqlite", database)
    db = CatalogDB(database)
    db.open()
    try:
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        local = db.get_artifact("legacy-local")
        blob = db.get_artifact("legacy-blob")
        assert local is not None and Path(local.storage_uri).read_text(encoding="utf-8").startswith("station")
        assert blob is not None and blob.storage_uri == "az://exampleaccount/example-container/archive/weather.csv"

        db.upsert_artifact(
            artifact_id=local.id,
            source_id="accepted-local",
            logical_path="assets/weather.csv",
            name="weather.csv",
            storage_uri=local.storage_uri,
            description="Mutated after migration",
            domain="weather",
            source_type="local",
            aliases=["legacy-local"],
        )
        exported_path = tmp_path / "rollback-v0.json"
        exported = json.loads(db.export_v0_json(exported_path))
        assert exported_path.exists()
        assert len(exported) == 2
        assert "legacy-local" in {record["id"] for record in exported}
        assert any(
            record["storage_uri"] == "az://exampleaccount/example-container/archive/weather.csv" for record in exported
        )
        assert next(record for record in exported if record["id"] == "legacy-local")["description"] == (
            "Mutated after migration"
        )
    finally:
        db.close()


@pytest.mark.integration
def test_keyword_only_acceptance_without_optional_cloud_or_vector_sdks():
    script = f"""
import importlib.abc
import asyncio
import json
import sys
from pathlib import Path

blocked = ("azure", "openai", "sqlite_vec")
class Blocked(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + ".") for name in blocked):
            raise ModuleNotFoundError(fullname, name=fullname)
        return None
sys.meta_path.insert(0, Blocked())

from agora_workbench.data_lake import RequestContext, SearchRequest
from agora_workbench.data_lake.catalog import CatalogConfig, ManifestCatalogProvider, SourceConfig

fixture = Path({str(FIXTURES)!r})
config = CatalogConfig(sources=[SourceConfig(
    source_id="fixture",
    path=str(fixture),
    discovery="manifest",
    manifest="manifest.json",
)])
async def main():
    provider = ManifestCatalogProvider(config)
    try:
        await provider.load()
        result = await provider.search(SearchRequest("weather"), RequestContext())
        assert result.items[0].presentation.name == "weather.csv"
    finally:
        await provider.aclose()
asyncio.run(main())
assert not any(any(name == root or name.startswith(root + ".") for root in blocked) for name in sys.modules)
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


@pytest.mark.performance
async def test_supported_offline_catalog_budgets(tmp_path):
    """Keep deterministic CI coverage below documented 1,000-item budgets."""
    root = tmp_path / "budget"
    data = root / "data"
    data.mkdir(parents=True)
    artifacts = []
    for index in range(1_000):
        name = f"observation-{index:04d}.csv"
        (data / name).write_text(f"station,value\n{index},{index % 17}\n", encoding="utf-8")
        artifacts.append(
            {
                "path": f"data/{name}",
                "artifact_id": f"observation-{index:04d}",
                "name": name,
                "description": "synthetic release acceptance observation",
                "domain": "budget",
                "media_type": "text/csv",
            }
        )
    manifest = root / "manifest.json"
    manifest.write_text(
        json.dumps({"version": 1, "generation": 1, "artifacts": artifacts}),
        encoding="utf-8",
    )
    provider = ManifestCatalogProvider(
        CatalogConfig(
            sources=[
                SourceConfig(
                    source_id="budget",
                    path=str(root),
                    discovery="manifest",
                    manifest="manifest.json",
                    max_stale_seconds=300,
                )
            ]
        ),
        db_path=tmp_path / "budget.db",
    )

    tracemalloc.start()
    started = time.perf_counter()
    try:
        assert await provider.load() == 1_000
        initial_refresh_seconds = time.perf_counter() - started
        _, peak_bytes = tracemalloc.get_traced_memory()

        latencies = []
        from agora_workbench.data_lake import SearchRequest

        for _ in range(20):
            search_started = time.perf_counter()
            page = await provider.search(SearchRequest("observation"), RequestContext())
            latencies.append(time.perf_counter() - search_started)
            assert page.items

        manifest.write_text(
            json.dumps({"version": 1, "generation": 2, "artifacts": artifacts}),
            encoding="utf-8",
        )
        refresh_started = time.perf_counter()
        assert await provider.load() == 1_000
        convergence_seconds = time.perf_counter() - refresh_started
        assert provider.readiness().sources[0].manifest_generation == 2

        assert initial_refresh_seconds < 15
        assert convergence_seconds < 10
        assert statistics.median(latencies) < 0.25
        assert peak_bytes < 128 * 1024 * 1024
    finally:
        tracemalloc.stop()
        await provider.aclose()


@pytest.mark.integration
async def test_invalid_startup_fails_closed_and_shutdown_is_idempotent(tmp_path):
    root = tmp_path / "invalid"
    root.mkdir()
    integration = CatalogIntegration.development_from_config(
        CatalogConfig(sources=[_manifest_source(root, "invalid")]),
        db_path=tmp_path / "invalid.db",
    )
    with pytest.raises(RuntimeError, match="not ready"):
        await integration.startup()
    await integration.shutdown()
    await integration.shutdown()
