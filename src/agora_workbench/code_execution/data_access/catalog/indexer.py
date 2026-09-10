"""Server-side catalog indexer — scans sources and populates the SQLite catalog."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from .identity import (
    azure_uri_from_blob_name,
    canonicalize_azure_uri,
    logical_artifact_id,
    normalize_logical_path,
    parse_azure_uri,
    sanitize_uri_for_display,
    stable_source_id,
)

from .config import CatalogConfig, SourceConfig
from .db import ArtifactRecord, CatalogDB, artifact_id_from_uri
from .embeddings import EmbeddingProvider, create_embedding_provider

LOGGER = logging.getLogger(__name__)

# Batch size for embedding computation
_EMBEDDING_BATCH_SIZE = 64

# Max concurrent blob source enumerations
_MAX_BLOB_CONCURRENCY = 8


if TYPE_CHECKING:
    from azure.core.credentials_async import AsyncTokenCredential

    from ...auth import CredentialProvider


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


def _source_id(source: SourceConfig) -> str:
    """Return the configured logical source ID or a compatibility fallback."""
    root = canonicalize_azure_uri(source.path) if source.source_type == "blob" else str(Path(source.path).resolve())
    return source.source_id or stable_source_id(source.source_type, root)


def _revision_digest(parts: object) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _configured_id_alias(custom_id: str) -> str:
    return custom_id if ":" in custom_id else f"artifact-id:{custom_id}"


def _parse_blob_path(path: str) -> tuple[str, str, str]:
    """Parse a blob storage path into (account, container, prefix).

    Supports az://, Blob/DFS HTTPS, and abfss:// forms.
    """
    if "://" not in path:
        raise ValueError("Not a blob source.")
    return parse_azure_uri(path)


@dataclass
class _EnumerationResult:
    artifacts: list[dict]
    successful_source_ids: set[str]


class CatalogIndexer:
    """Scans configured sources and populates the catalog database."""

    def __init__(
        self,
        config: CatalogConfig,
        db: CatalogDB,
        credential_provider: Optional[CredentialProvider] = None,
        embedding_provider: Optional[EmbeddingProvider] = None,
    ):
        """
        Args:
            config: Catalog configuration.
            db: The catalog database instance.
            credential_provider: Optional CredentialProvider from the auth module.
                Used for authenticating to blob storage. If None, blob sources
                will fall back to DefaultAzureCredential.
            embedding_provider: Optional pre-configured EmbeddingProvider instance.
                If provided, bypasses the factory and uses this provider directly.
                Useful for custom embedding backends not covered by the built-in
                factory (e.g. Cohere, Ollama, HuggingFace Inference API).
        """
        self._config = config
        self._db = db
        self._credential_provider = credential_provider
        self._embedding_provider: Optional[EmbeddingProvider] = embedding_provider

    @property
    def embedding_provider(self) -> Optional[EmbeddingProvider]:
        """Resolve the embedding provider from config (``None`` = keyword-only).

        Re-resolves while unset; a ``None`` result (keyword-only / BM25) is cheap
        to recompute, and tests may inject ``_embedding_provider`` directly.
        """
        if self._embedding_provider is None:
            search_cfg = self._config.search
            self._embedding_provider = create_embedding_provider(
                model_name=search_cfg.embedding_model,
                azure_openai_endpoint=search_cfg.azure_openai_endpoint,
                azure_openai_deployment=search_cfg.azure_openai_deployment,
                credential_provider=self._credential_provider,
                dimensions=search_cfg.embedding_dimensions,
            )
        provider_dimensions = self._embedding_provider.dimensions if self._embedding_provider is not None else None
        db_dimensions = self._db.vec_dimensions
        if provider_dimensions is not None and db_dimensions is not None and provider_dimensions != db_dimensions:
            raise ValueError(
                "Embedding provider dimension mismatch: "
                f"provider returns {provider_dimensions}, but CatalogDB expects {db_dimensions}. "
                "Construct CatalogDB with vec_dimensions=config.search.embedding_dimensions."
            )
        return self._embedding_provider

    async def index(self) -> int:
        """
        Run a full index pass: enumerate all sources (local + blob),
        diff against existing entries, compute embeddings, upsert.

        Returns:
            Number of artifacts indexed (new + updated).
        """
        enumeration = await self._enumerate_all_sources()
        all_artifacts = enumeration.artifacts

        discovered_by_source: dict[str, set[str]] = {
            source_id: set() for source_id in enumeration.successful_source_ids
        }
        for artifact in all_artifacts:
            discovered_by_source[artifact["source_id"]].add(artifact["logical_path"])

        records_by_source: dict[str, dict[str, ArtifactRecord]] = {}
        stale_ids: list[str] = []
        for source_id, discovered_paths in discovered_by_source.items():
            current = self._db.records_by_source_path(source_id, include_deleted=True)
            records_by_source[source_id] = current
            stale_ids.extend(
                record.id
                for path, record in current.items()
                if record.deleted_at is None and path not in discovered_paths
            )
        if stale_ids:
            self._db.delete_artifacts(stale_ids)
            LOGGER.info("Tombstoned %d stale artifacts.", len(stale_ids))

        to_index = []
        for artifact in all_artifacts:
            existing = records_by_source[artifact["source_id"]].get(artifact["logical_path"])
            if (
                existing is None
                or existing.deleted_at is not None
                or existing.storage_uri != artifact["storage_uri"]
                or existing.content_revision != artifact["content_revision"]
                or existing.metadata_revision != artifact["metadata_revision"]
            ):
                to_index.append(artifact)

        if not to_index:
            LOGGER.info("Catalog up to date (%d artifacts).", len(all_artifacts))
            return 0

        # Compute embeddings in batches and upsert within a transaction
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

    async def _enumerate_all_sources(self) -> _EnumerationResult:
        """Enumerate sources while retaining which sources completed successfully."""
        artifacts: list[dict] = []
        successful_source_ids: set[str] = set()
        blob_sources: list[SourceConfig] = []

        for source in self._config.sources:
            if source.source_type == "local":
                source_artifacts, succeeded = self._enumerate_local(source)
                artifacts.extend(source_artifacts)
                if succeeded:
                    successful_source_ids.add(_source_id(source))
            elif source.source_type == "blob":
                blob_sources.append(source)

        if blob_sources:
            blob_result = await self._enumerate_blob_sources_concurrent(blob_sources)
            artifacts.extend(blob_result.artifacts)
            successful_source_ids.update(blob_result.successful_source_ids)

        return _EnumerationResult(artifacts, successful_source_ids)

    async def _enumerate_blob_sources_concurrent(self, sources: list[SourceConfig]) -> _EnumerationResult:
        """Enumerate multiple blob sources concurrently with shared credential."""
        try:
            from azure.storage.blob.aio import BlobServiceClient
        except ImportError as exc:
            raise RuntimeError("Azure Blob catalog sources require the 'agora-workbench[azure]' extra.") from exc

        # Use the shared auth CredentialProvider if available, else fall back
        credential: AsyncTokenCredential
        owns_credential = False
        if self._credential_provider is not None:
            try:
                from ...auth import CredentialProviderTokenCredential
            except ImportError as exc:
                raise RuntimeError("Azure Blob catalog sources require the 'agora-workbench[azure]' extra.") from exc
            credential = CredentialProviderTokenCredential(self._credential_provider)
        else:
            try:
                from azure.identity.aio import DefaultAzureCredential
            except ImportError as exc:
                raise RuntimeError("Azure Blob catalog sources require the 'agora-workbench[azure]' extra.") from exc

            credential = DefaultAzureCredential()
            owns_credential = True

        # Reuse clients per storage account to avoid redundant connections
        clients: dict[str, BlobServiceClient] = {}
        semaphore = asyncio.Semaphore(_MAX_BLOB_CONCURRENCY)

        async def enumerate_one(source: SourceConfig) -> list[dict]:
            async with semaphore:
                return await self._enumerate_blob_source(source, credential, clients)

        try:
            tasks = [enumerate_one(source) for source in sources]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            artifacts: list[dict] = []
            successful_source_ids: set[str] = set()
            for source, result in zip(sources, results):
                if isinstance(result, BaseException):
                    LOGGER.error(
                        "Failed to enumerate blob source '%s' (%s)",
                        sanitize_uri_for_display(source.path),
                        type(result).__name__,
                    )
                else:
                    artifacts.extend(result)
                    successful_source_ids.add(_source_id(source))
            return _EnumerationResult(artifacts, successful_source_ids)
        finally:
            for client in clients.values():
                await client.close()
            # Only close credential if we created it (not shared from auth module)
            if owns_credential and hasattr(credential, "close"):
                await credential.close()

    async def _enumerate_blob_source(
        self,
        source: SourceConfig,
        credential: AsyncTokenCredential,
        clients: dict,
    ) -> list[dict]:
        """
        Enumerate blobs in a single Azure Blob Storage prefix.

        Uses shared credential and client pool for efficiency.
        """
        from azure.storage.blob.aio import BlobServiceClient

        account, container, prefix = _parse_blob_path(source.path)
        source_id = _source_id(source)
        source_root = canonicalize_azure_uri(source.path).rstrip("/")
        service_url = f"https://{account}.blob.core.windows.net"

        # Reuse BlobServiceClient per account
        if service_url not in clients:
            clients[service_url] = BlobServiceClient(service_url, credential=credential)
        client = clients[service_url]

        artifacts: list[dict] = []
        now = datetime.now(timezone.utc).isoformat()

        container_client = client.get_container_client(container)
        async for blob in container_client.list_blobs(name_starts_with=prefix):
            if prefix and not blob.name.startswith(prefix):
                continue
            if blob.name.endswith("/"):
                continue
            filename = blob.name.split("/")[-1]
            if filename.startswith("."):
                continue

            storage_uri = azure_uri_from_blob_name(account, container, blob.name)
            logical_path = normalize_logical_path(blob.name[len(prefix) :].lstrip("/") if prefix else blob.name)

            description = source.description
            domain = source.domain
            custom_id = None
            custom_aliases: list[str] = []
            if source.files:
                override = source.files.get(logical_path) or source.files.get(filename)
                if override:
                    if override.description:
                        description = override.description
                    if override.domain:
                        domain = override.domain
                    custom_id = override.artifact_id
                    custom_aliases = override.aliases

            legacy_id = artifact_id_from_uri(storage_uri)
            artifact_id = (
                (self._db.resolve_scan_alias(custom_id, source_id) if custom_id else None)
                or self._db.resolve_scan_alias(legacy_id, source_id)
                or custom_id
                or logical_artifact_id(source_id, logical_path)
            )
            content_revision = str(blob.etag or _revision_digest([blob.size, blob.last_modified]))
            metadata_revision = _revision_digest(
                [
                    filename,
                    description,
                    domain,
                    "blob",
                    _infer_content_type(filename) or blob.content_settings.content_type,
                    custom_id,
                    custom_aliases,
                ]
            )

            artifacts.append(
                {
                    "artifact_id": artifact_id,
                    "source_id": source_id,
                    "logical_path": logical_path,
                    "source_root": source_root,
                    "name": filename,
                    "storage_uri": storage_uri,
                    "description": description,
                    "domain": domain,
                    "source_type": "blob",
                    "content_type": _infer_content_type(filename) or blob.content_settings.content_type,
                    "size_bytes": blob.size,
                    "indexed_at": now,
                    "content_revision": content_revision,
                    "metadata_revision": metadata_revision,
                    "aliases": [
                        f"artifact-id:{legacy_id}",
                        f"blob:{legacy_id}",
                        f"storage-uri:{storage_uri}",
                        *([_configured_id_alias(custom_id)] if custom_id else []),
                        *custom_aliases,
                    ],
                }
            )

        return artifacts

    def _enumerate_local(self, source: SourceConfig) -> tuple[list[dict], bool]:
        """Walk a local directory and produce artifact records."""
        source_path = Path(source.path).resolve()
        source_id = _source_id(source)
        if not source_path.exists():
            LOGGER.warning("Source path does not exist: %s", source_path)
            return [], False

        artifacts: list[dict] = []
        now = datetime.now(timezone.utc).isoformat()
        errors: list[OSError] = []
        try:
            if source_path.is_file():
                artifacts.append(
                    self._make_local_artifact(
                        source_path,
                        source_path.name,
                        source_path.parent,
                        source_id,
                        source,
                        now,
                    )
                )
            else:
                for root, _dirs, files in os.walk(source_path, onerror=errors.append):
                    for filename in files:
                        if filename.startswith("."):
                            continue
                        filepath = Path(root) / filename
                        artifacts.append(
                            self._make_local_artifact(filepath, filename, source_path, source_id, source, now)
                        )
        except OSError as exc:
            errors.append(exc)
        if errors:
            LOGGER.error("Failed to enumerate local source '%s' (%s)", source_path, type(errors[0]).__name__)
            return [], False
        return artifacts, True

    def _make_local_artifact(
        self,
        filepath: Path,
        filename: str,
        source_root: Path,
        source_id: str,
        source: SourceConfig,
        indexed_at: str,
    ) -> dict:
        """Build an artifact dict from a local file."""
        storage_uri = str(filepath)
        logical_path = normalize_logical_path(str(filepath.relative_to(source_root)))
        rel_name = filename

        # Check for per-file overrides
        description = source.description
        domain = source.domain
        custom_id = None
        custom_aliases: list[str] = []
        if source.files:
            override = source.files.get(logical_path) or source.files.get(filename)
            if override:
                if override.description:
                    description = override.description
                if override.domain:
                    domain = override.domain
                custom_id = override.artifact_id
                custom_aliases = override.aliases

        stat = filepath.stat()
        content_type = _infer_content_type(filename)
        legacy_id = artifact_id_from_uri(storage_uri)
        artifact_id = (
            (self._db.resolve_scan_alias(custom_id, source_id) if custom_id else None)
            or self._db.resolve_scan_alias(legacy_id, source_id)
            or custom_id
            or logical_artifact_id(source_id, logical_path)
        )

        return {
            "artifact_id": artifact_id,
            "source_id": source_id,
            "logical_path": logical_path,
            "source_root": str(source_root),
            "name": rel_name,
            "storage_uri": storage_uri,
            "description": description,
            "domain": domain,
            "source_type": "local",
            "content_type": content_type,
            "size_bytes": stat.st_size,
            "indexed_at": indexed_at,
            "content_revision": _revision_digest([stat.st_size, stat.st_mtime_ns]),
            "metadata_revision": _revision_digest(
                [rel_name, description, domain, "local", content_type, custom_id, custom_aliases]
            ),
            "aliases": [
                f"artifact-id:{legacy_id}",
                f"local:{legacy_id}",
                f"storage-uri:{storage_uri}",
                *([_configured_id_alias(custom_id)] if custom_id else []),
                *custom_aliases,
            ],
        }

    async def _compute_and_store(self, artifacts: list[dict]) -> None:
        """Compute embeddings and upsert artifacts into the database in a transaction."""
        provider = self.embedding_provider
        if provider is None:
            # Keyword-only (BM25) catalog — no vector embeddings.
            all_embeddings: list[Optional[list[float]]] = [None] * len(artifacts)
        else:
            texts = [_build_indexable_text(a["name"], a.get("description"), a.get("domain")) for a in artifacts]
            all_embeddings = []
            resolved_dimensions = self._db.vec_dimensions or provider.dimensions
            for i in range(0, len(texts), _EMBEDDING_BATCH_SIZE):
                batch = texts[i : i + _EMBEDDING_BATCH_SIZE]
                embeddings = await provider.embed(batch)
                if len(embeddings) != len(batch):
                    raise ValueError(
                        f"Embedding provider returned {len(embeddings)} vectors for a batch of {len(batch)} texts."
                    )
                for embedding in embeddings:
                    if resolved_dimensions is None:
                        resolved_dimensions = len(embedding)
                    if len(embedding) != resolved_dimensions:
                        raise ValueError(
                            "Embedding provider dimension mismatch: "
                            f"CatalogDB expects {resolved_dimensions}, got {len(embedding)}. "
                            "Construct CatalogDB with vec_dimensions=config.search.embedding_dimensions."
                        )
                all_embeddings.extend(embeddings)

        rows = []
        for artifact, embedding in zip(artifacts, all_embeddings):
            rows.append({**artifact, "embedding": embedding})
        self._db.upsert_artifacts_batch(rows)
