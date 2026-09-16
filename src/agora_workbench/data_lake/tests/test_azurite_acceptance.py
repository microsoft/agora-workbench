"""Opt-in actual-Azurite acceptance for Blob managed storage."""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
import uuid
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from agora_workbench.code_execution import CatalogIntegration, CodeExecutionServer, ServerConfig
from agora_workbench.code_execution.auth import create_noop_auth_config
from agora_workbench.code_execution.data_access import manager as manager_module
from agora_workbench.code_execution.data_access.fetchers import BlobFetcher
from agora_workbench.code_execution.data_access.manager import DataLakeDataManager
from agora_workbench.code_execution.sessions import SessionConfig, SessionManager
from agora_workbench.data_lake import (
    BlobManagedStorage,
    DevelopmentAllowAllCatalogAuthorizer,
    ManagedCatalogWriter,
    RegisterArtifactRequest,
    ResourceLease,
    UploadArtifactRequest,
)
from agora_workbench.data_lake.catalog import CatalogDB
from agora_workbench.data_lake.providers import SQLiteCatalogProvider


def _azurite_connection_string() -> str:
    connection_string = os.getenv("AGORA_AZURITE_CONNECTION_STRING")
    if not connection_string:
        pytest.skip("Set AGORA_AZURITE_CONNECTION_STRING to run actual Azurite acceptance.")
    return connection_string


def _server_config(tmp_path: Path) -> ServerConfig:
    return ServerConfig(
        name="azurite-acceptance",
        description="Azurite catalog acceptance",
        type="uv",
        dependency_file="[project]\nname='azurite-acceptance'\nversion='0.0.0'\n",
        build_dir=tmp_path / "environment",
        auto_build=False,
    )


async def _create_container(container) -> None:
    http_errors = pytest.importorskip("azure.core.exceptions")
    try:
        await container.create_container()
    except http_errors.HttpResponseError as exc:
        if exc.error_code == "InvalidHeaderValue" and "API version" in str(exc):
            pytest.fail(
                "Azurite rejected the Azure SDK service API version. Start the pinned emulator with "
                "'azurite-blob --blobHost 0.0.0.0 --skipApiVersionCheck' as documented in "
                "docs/guide/data-lake-support.md.",
                pytrace=False,
            )
        raise


@pytest.mark.azurite
@pytest.mark.integration
async def test_azurite_blob_cas_transfer_interruption_and_reconciliation(tmp_path: Path):
    blob_module = pytest.importorskip("azure.storage.blob.aio")
    async with AsyncExitStack() as cleanup:
        service = blob_module.BlobServiceClient.from_connection_string(_azurite_connection_string())
        cleanup.push_async_callback(service.close)
        container_name = f"agora-acceptance-{uuid.uuid4().hex}"
        container = service.get_container_client(container_name)
        await _create_container(container)
        cleanup.push_async_callback(container.delete_container)

        prefix = f"release/{uuid.uuid4().hex}"
        first_writer = ManagedCatalogWriter("azurite", BlobManagedStorage(container, prefix=prefix))
        second_writer = ManagedCatalogWriter("azurite", BlobManagedStorage(container, prefix=prefix))

        payloads = []
        for index in range(4):
            payload = tmp_path / f"payload-{index}.txt"
            payload.write_text(f"azurite-{index}", encoding="utf-8")
            payloads.append(payload)
        results = await asyncio.gather(
            *[
                (first_writer if index % 2 == 0 else second_writer).upload(
                    UploadArtifactRequest(
                        operation_id=f"writer-{index}",
                        path=f"artifacts/{index}.txt",
                        local_path=payload,
                    )
                )
                for index, payload in enumerate(payloads)
            ]
        )
        assert {result.generation for result in results} == {1, 2, 3, 4}
        manifest = await first_writer.read_manifest(minimum_generation=4)
        assert manifest.generation == 4

        committed = manifest.artifacts[0]
        download = await container.download_blob(f"{prefix}/{committed.storage_path}")
        assert await download.readall() in {payload.read_bytes() for payload in payloads}

        external = b"registered-through-blob-transfer"
        await container.upload_blob(f"{prefix}/incoming/external.bin", external)
        registered = await second_writer.register(
            RegisterArtifactRequest(
                operation_id="register-external",
                path="artifacts/external.bin",
                storage_path="incoming/external.bin",
                checksum_sha256=hashlib.sha256(external).hexdigest(),
            )
        )
        assert registered.generation == 5

        def interrupt(step: str, operation_id: str) -> None:
            if step == "after_object" and operation_id == "interrupted":
                raise RuntimeError("simulated Azurite interruption")

        interrupted_writer = ManagedCatalogWriter(
            "azurite",
            BlobManagedStorage(container, prefix=prefix),
            interruption_hook=interrupt,
        )
        with pytest.raises(RuntimeError, match="simulated Azurite interruption"):
            await interrupted_writer.upload(
                UploadArtifactRequest(
                    operation_id="interrupted",
                    path="artifacts/interrupted.txt",
                    local_path=payloads[0],
                )
            )
        report = await first_writer.reconcile(grace_seconds=0)
        assert report.removed_orphans == ("interrupted",)
        assert await first_writer.read_manifest(minimum_generation=5)


@pytest.mark.azurite
@pytest.mark.integration
async def test_azurite_public_catalog_mcp_execution_roundtrip(tmp_path: Path, monkeypatch):
    blob_module = pytest.importorskip("azure.storage.blob.aio")
    credential_module = pytest.importorskip("azure.core.credentials")
    async with AsyncExitStack() as cleanup:
        service = blob_module.BlobServiceClient.from_connection_string(_azurite_connection_string())
        cleanup.push_async_callback(service.close)
        container_name = f"agora-public-{uuid.uuid4().hex}"
        container = service.get_container_client(container_name)
        await _create_container(container)
        cleanup.push_async_callback(container.delete_container)

        prefix = f"catalog/{uuid.uuid4().hex}"
        blob_name = f"{prefix}/approved/observations.csv"
        payload = b"station,value\nSEA,42\n"
        await container.upload_blob(blob_name, payload)

        sdk_policy = service.credential
        credential = credential_module.AzureNamedKeyCredential(sdk_policy.account_name, sdk_policy.account_key)
        endpoint = service.url.rstrip("/")
        database = CatalogDB(tmp_path / "azurite-catalog.db")
        database.open()
        cleanup.callback(database.close)
        database.upsert_artifact(
            artifact_id="azurite-observations",
            source_id="azurite",
            logical_path="approved/observations.csv",
            name="azurite observations.csv",
            storage_uri=f"az://devstoreaccount1/{container_name}/{blob_name}",
            description="Actual emulator-backed observations",
            domain="acceptance",
            source_type="blob",
        )
        provider = SQLiteCatalogProvider(database, ("azurite",))
        integration = CatalogIntegration(
            ResourceLease(provider),
            authorizer=DevelopmentAllowAllCatalogAuthorizer(),
        )
        cleanup.push_async_callback(integration.shutdown)

        cache_index = 0

        def isolated_cache(*, prefix: str) -> str:
            nonlocal cache_index
            cache_index += 1
            path = tmp_path / f"{prefix}{cache_index}"
            path.mkdir()
            return str(path)

        monkeypatch.setattr(manager_module.tempfile, "mkdtemp", isolated_cache)

        def manager_factory(_context):
            return DataLakeDataManager(
                extra_fetchers=[
                    BlobFetcher(
                        credential=cast(Any, credential),
                        allowed_locations=[f"az://devstoreaccount1/{container_name}/{prefix}/"],
                        account_endpoints={"devstoreaccount1": endpoint},
                    )
                ],
                credential=cast(Any, credential),
            )

        session_manager = SessionManager(SessionConfig(data_manager_factory=manager_factory))
        cleanup.push_async_callback(session_manager.aclose_all_sessions)
        server = CodeExecutionServer(
            _server_config(tmp_path),
            auth_config=create_noop_auth_config(),
            session_manager=session_manager,
            catalog=integration,
        )
        await integration.startup()
        session_id = session_manager.create_session(
            {},
            "azurite-user@tenant",
            "azurite-token",
            {"oid": "azurite-user", "tid": "tenant", "exp": int(time.time()) + 3600},
        )

        search_tool = await server.mcp.get_tool("search_data")
        hits = await search_tool.fn(
            query="azurite observations",
            top=1,
            mcp_ctx=SimpleNamespace(session_id=session_id),
        )
        assert [hit["id"] for hit in hits] == ["azurite-observations"]
        assert hits[0]["source_id"] == "azurite"
        assert hits[0]["load_path"].startswith("<blob>catalog-v1:")

        manager = session_manager.get_session(session_id).data_manager
        assert not manager._catalog_managed_revision_access
        cached = await manager.get_cache_path(hits[0]["load_path"])
        assert cached.read_bytes() == payload
