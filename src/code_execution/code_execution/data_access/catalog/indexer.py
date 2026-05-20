"""Server-side catalog indexer — scans sources and populates the SQLite catalog."""

from __future__ import annotations

import logging
import mimetypes
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import CatalogConfig, SourceConfig
from .db import CatalogDB, artifact_id_from_uri
from .embeddings import EmbeddingProvider, create_embedding_provider

LOGGER = logging.getLogger(__name__)

# Batch size for embedding computation
_EMBEDDING_BATCH_SIZE = 64


def _build_indexable_text(name: str, description: Optional[str], domain: Optional[str]) -> str:
    """Build the text string used for embedding computation."""
    parts = [name]
    if description:
        parts.append(description)
    if domain:
        parts.append(domain)
    return " ".join(parts)


def _infer_content_type(filename: str) -> Optional[str]:
    """Infer MIME type from filename."""
    content_type, _ = mimetypes.guess_type(filename)
    return content_type


def _parse_blob_path(path: str) -> tuple[str, str, str]:
    """Parse a blob storage path into (account, container, prefix).

    Supports:
      - az://account/container/prefix
      - https://<account>.blob.core.windows.net/container/prefix
    """
    if path.startswith("az://"):
        parts = path[len("az://") :].split("/", 2)
        account = parts[0]
        container = parts[1] if len(parts) > 1 else ""
        prefix = parts[2] if len(parts) > 2 else ""
        return account, container, prefix

    if ".blob.core.windows.net" in path:
        from urllib.parse import urlparse

        parsed = urlparse(path)
        # hostname: <account>.blob.core.windows.net
        account = parsed.hostname.split(".")[0] if parsed.hostname else ""
        # path: /container/prefix/...
        path_parts = parsed.path.lstrip("/").split("/", 1)
        container = path_parts[0] if path_parts else ""
        prefix = path_parts[1] if len(path_parts) > 1 else ""
        return account, container, prefix

    raise ValueError(f"Not a blob source: {path}")


class CatalogIndexer:
    """Scans configured sources and populates the catalog database."""

    def __init__(self, config: CatalogConfig, db: CatalogDB):
        self._config = config
        self._db = db
        self._embedding_provider: Optional[EmbeddingProvider] = None

    @property
    def embedding_provider(self) -> EmbeddingProvider:
        """Lazy-initialize the embedding provider."""
        if self._embedding_provider is None:
            search_cfg = self._config.search
            self._embedding_provider = create_embedding_provider(
                model_name=search_cfg.embedding_model,
                azure_openai_endpoint=search_cfg.azure_openai_endpoint,
                azure_openai_deployment=search_cfg.azure_openai_deployment,
            )
        return self._embedding_provider

    async def index(self) -> int:
        """
        Run a full index pass: enumerate files, diff, compute embeddings, upsert.

        Returns:
            Number of artifacts indexed (new + updated).
        """
        all_artifacts = self._enumerate_sources()

        if not all_artifacts:
            LOGGER.info("No artifacts found in configured sources.")
            return 0

        # Diff against existing entries
        existing_uris = self._db.get_existing_uris()
        new_uris = {a["storage_uri"] for a in all_artifacts}

        # Remove artifacts no longer in sources
        stale_uris = existing_uris - new_uris
        if stale_uris:
            stale_ids = [artifact_id_from_uri(uri) for uri in stale_uris]
            self._db.delete_artifacts(stale_ids)
            LOGGER.info("Removed %d stale artifacts.", len(stale_ids))

        # Determine which artifacts need (re-)indexing
        to_index = [a for a in all_artifacts if a["storage_uri"] not in existing_uris]

        if not to_index:
            LOGGER.info("Catalog up to date (%d artifacts).", len(all_artifacts))
            return 0

        # Compute embeddings in batches
        await self._compute_and_store(to_index)

        # Log warning for artifacts without descriptions
        no_desc_count = sum(1 for a in all_artifacts if not a.get("description"))
        if no_desc_count:
            LOGGER.warning(
                "%d artifacts have no description — search quality will be reduced.",
                no_desc_count,
            )

        LOGGER.info("Indexed %d new artifacts (%d total).", len(to_index), len(all_artifacts))
        return len(to_index)

    def _enumerate_sources(self) -> list[dict]:
        """Enumerate files from all configured sources."""
        artifacts: list[dict] = []
        for source in self._config.sources:
            if source.source_type == "local":
                artifacts.extend(self._enumerate_local(source))
            elif source.source_type == "blob":
                LOGGER.warning(
                    "Blob source enumeration at index time requires async. "
                    "Blob source '%s' will be skipped during sync enumeration. "
                    "Use index_blob_source() for async blob indexing.",
                    source.path,
                )
        return artifacts

    def _enumerate_local(self, source: SourceConfig) -> list[dict]:
        """Walk a local directory and produce artifact records."""
        source_path = Path(source.path).resolve()
        if not source_path.exists():
            LOGGER.warning("Source path does not exist: %s", source_path)
            return []

        artifacts: list[dict] = []
        now = datetime.now(timezone.utc).isoformat()

        if source_path.is_file():
            artifacts.append(self._make_local_artifact(source_path, source_path.name, source, now))
        else:
            for root, _dirs, files in os.walk(source_path):
                for filename in files:
                    if filename.startswith("."):
                        continue
                    filepath = Path(root) / filename
                    artifacts.append(self._make_local_artifact(filepath, filename, source, now))

        return artifacts

    def _make_local_artifact(self, filepath: Path, filename: str, source: SourceConfig, indexed_at: str) -> dict:
        """Build an artifact dict from a local file."""
        storage_uri = str(filepath)
        artifact_id = artifact_id_from_uri(storage_uri)
        rel_name = filename

        # Check for per-file overrides
        description = source.description
        domain = source.domain
        if source.files:
            relative = str(filepath.relative_to(Path(source.path).resolve()))
            override = source.files.get(relative) or source.files.get(filename)
            if override:
                if override.description:
                    description = override.description
                if override.domain:
                    domain = override.domain

        return {
            "artifact_id": artifact_id,
            "name": rel_name,
            "storage_uri": storage_uri,
            "description": description,
            "domain": domain,
            "source_type": "local",
            "content_type": _infer_content_type(filename),
            "size_bytes": filepath.stat().st_size,
            "indexed_at": indexed_at,
        }

    async def index_blob_source(self, source: SourceConfig) -> list[dict]:
        """
        Enumerate blobs in an Azure Blob Storage prefix.

        Supports both az://account/container/prefix and
        https://<account>.blob.core.windows.net/container/prefix formats.

        Requires azure-storage-blob with async support.
        """
        account, container, prefix = _parse_blob_path(source.path)

        from azure.identity.aio import DefaultAzureCredential
        from azure.storage.blob.aio import BlobServiceClient

        credential = DefaultAzureCredential()
        service_url = f"https://{account}.blob.core.windows.net"
        client = BlobServiceClient(service_url, credential=credential)

        artifacts: list[dict] = []
        now = datetime.now(timezone.utc).isoformat()

        try:
            container_client = client.get_container_client(container)
            async for blob in container_client.list_blobs(name_starts_with=prefix):
                if blob.name.endswith("/"):
                    continue
                filename = blob.name.split("/")[-1]
                if filename.startswith("."):
                    continue

                storage_uri = f"az://{account}/{container}/{blob.name}"
                artifact_id = artifact_id_from_uri(storage_uri)

                description = source.description
                domain = source.domain
                if source.files:
                    rel = blob.name[len(prefix) :].lstrip("/") if prefix else blob.name
                    override = source.files.get(rel) or source.files.get(filename)
                    if override:
                        if override.description:
                            description = override.description
                        if override.domain:
                            domain = override.domain

                artifacts.append(
                    {
                        "artifact_id": artifact_id,
                        "name": filename,
                        "storage_uri": storage_uri,
                        "description": description,
                        "domain": domain,
                        "source_type": "blob",
                        "content_type": _infer_content_type(filename) or blob.content_settings.content_type,
                        "size_bytes": blob.size,
                        "indexed_at": now,
                    }
                )
        finally:
            await client.close()
            await credential.close()

        return artifacts

    async def _compute_and_store(self, artifacts: list[dict]) -> None:
        """Compute embeddings and upsert artifacts into the database."""
        # Build texts for embedding
        texts = [_build_indexable_text(a["name"], a.get("description"), a.get("domain")) for a in artifacts]

        # Compute embeddings in batches
        all_embeddings: list[list[float]] = []
        for i in range(0, len(texts), _EMBEDDING_BATCH_SIZE):
            batch = texts[i : i + _EMBEDDING_BATCH_SIZE]
            batch_embeddings = await self.embedding_provider.embed(batch)
            all_embeddings.extend(batch_embeddings)

        # Upsert with embeddings
        for artifact, embedding in zip(artifacts, all_embeddings):
            artifact["embedding"] = embedding
            self._db.upsert_artifact(**artifact)
