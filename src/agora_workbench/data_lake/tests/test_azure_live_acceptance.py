"""Explicitly configured live-Azure data-lake acceptance."""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from urllib.parse import urlparse

import pytest

from agora_workbench.code_execution.auth.base import AccessToken, CredentialProvider
from agora_workbench.data_lake import (
    BlobManagedStorage,
    ListRequest,
    ManagedCatalogWriter,
    RequestContext,
    UploadArtifactRequest,
)
from agora_workbench.data_lake.catalog import CatalogConfig, ManifestCatalogProvider, SourceConfig


def _live_settings() -> tuple[str, str, str, str]:
    values = (
        os.getenv("AGORA_LIVE_AZURE_ACCOUNT_URL", ""),
        os.getenv("AGORA_LIVE_AZURE_ALLOWED_CONTAINER", ""),
        os.getenv("AGORA_LIVE_AZURE_DENIED_CONTAINER", ""),
        os.getenv("AGORA_LIVE_AZURE_IDENTITY_MODE", ""),
    )
    if not all(values):
        pytest.skip(
            "Live Azure acceptance requires AGORA_LIVE_AZURE_ACCOUNT_URL, "
            "AGORA_LIVE_AZURE_ALLOWED_CONTAINER, AGORA_LIVE_AZURE_DENIED_CONTAINER, "
            "and AGORA_LIVE_AZURE_IDENTITY_MODE."
        )
    return values


def _credential(mode: str):
    identity = pytest.importorskip("azure.identity.aio")
    if mode == "managed":
        return identity.ManagedIdentityCredential(client_id=os.getenv("AGORA_LIVE_AZURE_MANAGED_CLIENT_ID") or None)
    if mode == "workload":
        return identity.WorkloadIdentityCredential()
    if mode == "delegated":
        return identity.AzureCliCredential(tenant_id=os.getenv("AGORA_LIVE_AZURE_TENANT_ID") or None)
    raise ValueError(f"Unsupported AGORA_LIVE_AZURE_IDENTITY_MODE: {mode}")


class _CredentialProvider(CredentialProvider):
    def __init__(self, credential) -> None:
        self.credential = credential

    async def get_token(self, scope: str) -> AccessToken:
        token = await self.credential.get_token(scope)
        return AccessToken(token.token, token.expires_on)

    async def close(self) -> None:
        # The test owns the SDK credential; CatalogIndexer owns only its adapter.
        return None


@pytest.mark.live
@pytest.mark.parametrize("identity_mode", ["managed", "workload", "delegated"])
async def test_live_azure_identity_rbac_catalog_and_blob_cas(tmp_path: Path, identity_mode: str):
    account_url, allowed_container_name, denied_container_name, configured_mode = _live_settings()
    if configured_mode != identity_mode:
        pytest.skip(f"Configured live identity mode is {configured_mode!r}, not {identity_mode!r}.")

    blob_module = pytest.importorskip("azure.storage.blob.aio")
    http_errors = pytest.importorskip("azure.core.exceptions")
    credential = _credential(identity_mode)
    allowed = blob_module.ContainerClient(account_url, allowed_container_name, credential=credential)
    denied = blob_module.ContainerClient(account_url, denied_container_name, credential=credential)
    prefix = f"agora-v030-acceptance/{uuid.uuid4().hex}"
    writer = ManagedCatalogWriter("live-azure", BlobManagedStorage(allowed, prefix=prefix))
    payload = tmp_path / "live.txt"
    payload.write_text("live Azure acceptance", encoding="utf-8")

    try:
        with pytest.raises(http_errors.HttpResponseError) as denied_error:
            await denied.get_container_properties()
        assert denied_error.value.status_code == 403

        first, second = await asyncio.gather(
            writer.upload(UploadArtifactRequest("live-first", "first.txt", payload)),
            ManagedCatalogWriter("live-azure", BlobManagedStorage(allowed, prefix=prefix)).upload(
                UploadArtifactRequest("live-second", "second.txt", payload)
            ),
        )
        assert {first.generation, second.generation} == {1, 2}

        account = urlparse(account_url).hostname.split(".", 1)[0]
        provider = ManifestCatalogProvider(
            CatalogConfig(
                sources=[
                    SourceConfig(
                        source_id="live-azure",
                        path=f"az://{account}/{allowed_container_name}/{prefix}",
                        discovery="manifest",
                        manifest=".agora/manifest.json",
                    )
                ]
            ),
            db_path=tmp_path / "live-azure.db",
            credential_provider=_CredentialProvider(credential),
        )
        try:
            assert await provider.load() == 2
            artifacts = (await provider.list(ListRequest(), RequestContext(caller_id="live"))).items
            assert {artifact.reference.artifact_id for artifact in artifacts} == {
                first.artifact_id,
                second.artifact_id,
            }
            assert provider.readiness().sources[0].manifest_generation == 2
        finally:
            await provider.aclose()
    finally:
        names = [item.name async for item in allowed.list_blobs(name_starts_with=f"{prefix}/")]
        if names:
            await asyncio.gather(*[allowed.delete_blob(name) for name in names])
        await allowed.close()
        await denied.close()
        await credential.close()
