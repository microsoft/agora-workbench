"""Opt-in actual-Azurite acceptance for Blob managed storage."""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from pathlib import Path

import pytest

from agora_workbench.data_lake import (
    BlobManagedStorage,
    ManagedCatalogWriter,
    RegisterArtifactRequest,
    UploadArtifactRequest,
)


def _azurite_connection_string() -> str:
    connection_string = os.getenv("AGORA_AZURITE_CONNECTION_STRING")
    if not connection_string:
        pytest.skip("Set AGORA_AZURITE_CONNECTION_STRING to run actual Azurite acceptance.")
    return connection_string


@pytest.mark.azurite
@pytest.mark.integration
async def test_azurite_blob_cas_transfer_interruption_and_reconciliation(tmp_path: Path):
    blob_module = pytest.importorskip("azure.storage.blob.aio")
    service = blob_module.BlobServiceClient.from_connection_string(_azurite_connection_string())
    container_name = f"agora-acceptance-{uuid.uuid4().hex}"
    container = service.get_container_client(container_name)
    await container.create_container()
    try:
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
    finally:
        await container.delete_container()
        await service.close()
