"""Server-side catalog indexer — scans sources and populates the SQLite catalog."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Mapping
from typing import TYPE_CHECKING, Optional
from urllib.parse import unquote

from agora_workbench.data_lake.manifest import (
    MAX_MANIFEST_BYTES,
    CatalogManifest,
    ManifestArtifact,
)

from .identity import (
    azure_uri_from_blob_name,
    canonicalize_azure_uri,
    is_reserved_provider_path,
    is_scan_excluded_path,
    logical_artifact_id,
    normalize_logical_path,
    parse_azure_uri,
    sanitize_uri_for_display,
    split_alias,
    stable_source_id,
)

from .config import CatalogConfig, DiscoveryMode, SourceConfig
from .db import ArtifactRecord, CatalogDB, artifact_id_from_uri
from .embeddings import EmbeddingProvider, create_embedding_provider

LOGGER = logging.getLogger(__name__)

# Batch size for embedding computation
_EMBEDDING_BATCH_SIZE = 64


def _open_directory_no_follow(path: Path) -> int:
    """Open an absolute directory by traversing every component without following symlinks."""
    if os.name != "posix" or not path.is_absolute():
        raise OSError("Secure local catalog traversal is unavailable on this platform.")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    current = os.open(os.path.sep, flags)
    try:
        for part in path.parts[1:]:
            next_fd = os.open(part, flags, dir_fd=current)
            os.close(current)
            current = next_fd
        return current
    except BaseException:
        os.close(current)
        raise


def _stat_regular_file_no_follow(path: str | Path, *, dir_fd: int | None = None) -> os.stat_result:
    """Atomically open and stat a regular file without following a final symlink."""
    parent_fd: int | None = None
    if dir_fd is None:
        absolute_path = Path(path)
        parent_fd = _open_directory_no_follow(absolute_path.parent)
        dir_fd = parent_fd
        path = absolute_path.name
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
            dir_fd=dir_fd,
        )
        try:
            result = os.fstat(descriptor)
            if not stat.S_ISREG(result.st_mode):
                raise OSError("Catalog source entry is not a regular file.")
            return result
        finally:
            os.close(descriptor)
    finally:
        if parent_fd is not None:
            os.close(parent_fd)


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
    return source.source_id or stable_source_id(source.source_type, _source_root(source))


def _source_root(source: SourceConfig) -> str:
    """Return the credential-free canonical root used for refresh ownership."""
    return canonicalize_azure_uri(source.path) if source.source_type == "blob" else str(Path(source.path).resolve())


def _revision_digest(parts: object) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _configured_id_alias(custom_id: str) -> str:
    return custom_id if ":" in custom_id else f"artifact-id:{custom_id}"


def _alias_canonical_candidates(namespace: str, alias: str) -> tuple[str, ...]:
    if namespace == "artifact-id":
        return alias, f"{namespace}:{alias}"
    return (f"{namespace}:{alias}",)


def _embedding_model_id(config: CatalogConfig) -> str:
    """Return a stable non-secret identifier for stored vector semantics."""
    model = config.search.embedding_model
    if model == "azure-openai":
        return f"{model}:{config.search.azure_openai_deployment}"
    return model


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
    errors: dict[str, str] = field(default_factory=dict)


class ManifestRefreshError(RuntimeError):
    """One or more manifest sources failed while other source updates may have committed."""

    def __init__(self, errors: dict[str, str]):
        self.errors = {source_id: errors[source_id] for source_id in sorted(errors)}
        self.source_ids = tuple(self.errors)
        details = "; ".join(f"{source_id}: {self.errors[source_id]}" for source_id in self.source_ids)
        super().__init__(f"Manifest refresh failed: {details}")


class _DuplicateManifestKeyError(ValueError):
    pass


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateManifestKeyError(f"Duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"Unsupported JSON numeric constant: {value}")


def _safe_source_error(exc: BaseException) -> str:
    error_type = type(exc).__name__.lower()
    if isinstance(exc, FileNotFoundError):
        category = "manifest_missing"
    elif isinstance(exc, PermissionError) or "credential" in error_type or "authentication" in error_type:
        category = "credential_or_access_failure"
    elif isinstance(exc, ValueError):
        category = "manifest_invalid"
    elif _exception_chain_contains(exc, ImportError):
        category = "optional_dependency_missing"
    else:
        category = "source_unavailable"
    return f"{category}: source refresh failed"


def _exception_chain_contains(exc: BaseException, error_type: type[BaseException]) -> bool:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        if isinstance(current, error_type):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


@dataclass(frozen=True)
class SourceDryRun:
    """Planned changes for one source without mutating SQLite."""

    source_id: str
    discovery: str
    added: int = 0
    updated: int = 0
    deleted: int = 0
    unchanged: int = 0
    manifest_generation: int | None = None
    manifest_etag: str | None = None
    configuration_valid: bool = True
    manifest_checked: bool = False
    manifest_content_valid: bool | None = None
    error: str | None = None


@dataclass(frozen=True)
class CatalogDryRunReport:
    """Mutation-free catalog refresh preview."""

    sources: tuple[SourceDryRun, ...]
    configuration_valid: bool = True

    @property
    def has_errors(self) -> bool:
        return any(source.error is not None for source in self.sources)


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
        self._manifest_revisions: dict[str, tuple[int, str]] = {}

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
        sources = self._validated_sources()
        self._manifest_revisions = {}
        enumeration = await self._enumerate_all_sources(sources)
        enumeration = self._isolate_manifest_candidate_conflicts(enumeration, sources)
        all_artifacts = enumeration.artifacts
        attempted_at = datetime.now(timezone.utc).isoformat()

        discovered_by_source: dict[str, set[str]] = {
            source_id: set() for source_id in enumeration.successful_source_ids
        }
        for artifact in all_artifacts:
            discovered_by_source.setdefault(artifact["source_id"], set()).add(artifact["logical_path"])

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

        provider = self.embedding_provider
        existing_live_ids = [
            record.id
            for records in records_by_source.values()
            for record in records.values()
            if record.deleted_at is None
        ]
        missing_vector_ids = (
            self._db.missing_vectors(existing_live_ids) if provider is not None and existing_live_ids else set()
        )
        to_index: list[tuple[dict, bool]] = []
        for artifact in all_artifacts:
            existing = records_by_source[artifact["source_id"]].get(artifact["logical_path"])
            needs_vector = (
                existing is not None
                and existing.deleted_at is None
                and provider is not None
                and existing.id in missing_vector_ids
            )
            if (
                existing is None
                or existing.deleted_at is not None
                or existing.storage_uri != artifact["storage_uri"]
                or existing.content_revision != artifact["content_revision"]
                or existing.metadata_revision != artifact["metadata_revision"]
                or needs_vector
            ):
                searchable_change = (
                    existing is None
                    or existing.deleted_at is not None
                    or existing.name != artifact["name"]
                    or existing.description != artifact["description"]
                    or existing.domain != artifact["domain"]
                    or needs_vector
                )
                to_index.append((artifact, provider is not None and searchable_change))

        rows = await self._compute_rows(to_index)
        artifact_counts: dict[str, int] = {}
        for artifact in all_artifacts:
            artifact_counts[artifact["source_id"]] = artifact_counts.get(artifact["source_id"], 0) + 1
        source_results = []
        for source, source_id in sources:
            succeeded = source_id in enumeration.successful_source_ids
            manifest_revision = self._manifest_revisions.get(source_id)
            source_results.append(
                {
                    "source_id": source_id,
                    "source_type": source.source_type,
                    "root_uri": (
                        canonicalize_azure_uri(source.path)
                        if source.source_type == "blob"
                        else str(Path(source.path).resolve())
                    ),
                    "attempted_at": attempted_at,
                    "succeeded": succeeded,
                    "artifact_count": artifact_counts.get(source_id, 0) if succeeded else None,
                    "error": None if succeeded else enumeration.errors.get(source_id, "Source enumeration failed"),
                    "manifest_generation": manifest_revision[0] if succeeded and manifest_revision else None,
                    "manifest_etag": manifest_revision[1] if succeeded and manifest_revision else None,
                }
            )

        vector_model_id = (
            _embedding_model_id(self._config) if provider is not None and self._db.vec_dimensions is not None else None
        )
        self._db.apply_refresh_batch(rows, stale_ids, source_results, vector_model_id)
        if stale_ids:
            LOGGER.info("Tombstoned %d stale artifacts.", len(stale_ids))

        manifest_errors = {
            source_id: error
            for source_id, error in enumeration.errors.items()
            if next(source.discovery for source, candidate_id in sources if candidate_id == source_id)
            is DiscoveryMode.MANIFEST
        }
        if manifest_errors:
            raise ManifestRefreshError(manifest_errors)

        # Log warning for artifacts without descriptions
        no_desc_count = sum(1 for a in all_artifacts if not a.get("description"))
        if no_desc_count:
            LOGGER.warning(
                "%d artifacts have no description — search quality will be reduced.",
                no_desc_count,
            )

        if not to_index:
            if enumeration.errors:
                LOGGER.warning(
                    "Catalog refresh preserved last valid state for %d failed source(s).",
                    len(enumeration.errors),
                )
            else:
                LOGGER.info("Catalog up to date (%d artifacts).", len(all_artifacts))
            return 0
        LOGGER.info("Indexed %d new or updated artifacts (%d total).", len(to_index), len(all_artifacts))
        return len(to_index)

    async def dry_run(self) -> CatalogDryRunReport:
        """Enumerate and diff sources without writing SQLite or computing embeddings."""
        sources = self._validated_sources()
        self._manifest_revisions = {}
        enumeration = await self._enumerate_all_sources(sources)
        enumeration = self._isolate_manifest_candidate_conflicts(enumeration, sources)
        artifacts_by_source: dict[str, list[dict]] = {}
        for artifact in enumeration.artifacts:
            artifacts_by_source.setdefault(artifact["source_id"], []).append(artifact)

        planned: list[SourceDryRun] = []
        for source, source_id in sources:
            error = enumeration.errors.get(source_id)
            revision = self._manifest_revisions.get(source_id)
            if error is not None:
                planned.append(
                    SourceDryRun(
                        source_id=source_id,
                        discovery=source.discovery.value,
                        manifest_generation=revision[0] if revision else None,
                        manifest_etag=revision[1] if revision else None,
                        manifest_checked=source.discovery is DiscoveryMode.MANIFEST,
                        manifest_content_valid=(False if source.discovery is DiscoveryMode.MANIFEST else None),
                        error=error,
                    )
                )
                continue
            existing = self._db.records_by_source_path(source_id, include_deleted=True)
            discovered = {artifact["logical_path"]: artifact for artifact in artifacts_by_source.get(source_id, [])}
            added = updated = unchanged = 0
            for logical_path, artifact in discovered.items():
                record = existing.get(logical_path)
                if record is None:
                    added += 1
                elif (
                    record.deleted_at is not None
                    or record.storage_uri != artifact["storage_uri"]
                    or record.content_revision != artifact["content_revision"]
                    or record.metadata_revision != artifact["metadata_revision"]
                ):
                    updated += 1
                else:
                    unchanged += 1
            deleted = sum(
                record.deleted_at is None and logical_path not in discovered
                for logical_path, record in existing.items()
            )
            planned.append(
                SourceDryRun(
                    source_id=source_id,
                    discovery=source.discovery.value,
                    added=added,
                    updated=updated,
                    deleted=deleted,
                    unchanged=unchanged,
                    manifest_generation=revision[0] if revision else None,
                    manifest_etag=revision[1] if revision else None,
                    manifest_checked=source.discovery is DiscoveryMode.MANIFEST,
                    manifest_content_valid=(True if source.discovery is DiscoveryMode.MANIFEST else None),
                )
            )
        return CatalogDryRunReport(tuple(planned))

    def _record_manifest_revision(self, source_id: str, generation: int, etag: str) -> None:
        state = self._db.get_source_refresh_state(source_id)
        if state is not None and state.manifest_generation is not None:
            if generation < state.manifest_generation:
                raise ValueError(
                    f"Manifest generation {generation} is older than cached generation {state.manifest_generation}"
                )
            if generation == state.manifest_generation and state.manifest_etag not in {None, etag}:
                raise ValueError("Manifest content changed without advancing its generation")
        self._manifest_revisions[source_id] = (generation, etag)

    def _validated_sources(self) -> list[tuple[SourceConfig, str]]:
        """Resolve normalized source identities and reject ambiguous duplicate refresh ownership."""
        sources: list[tuple[SourceConfig, str]] = []
        seen_ids: set[str] = set()
        seen_roots: set[tuple[str, str]] = set()
        for source in self._config.sources:
            source_id = _source_id(source)
            if source_id in seen_ids:
                raise ValueError(
                    f"Duplicate effective catalog source_id {source_id!r}; each configured source must be unique"
                )
            source_root = (source.source_type, _source_root(source))
            if source_root in seen_roots:
                raise ValueError(
                    f"Duplicate effective catalog source root {source_root[1]!r}; "
                    "each configured source must have unique refresh ownership"
                )
            seen_ids.add(source_id)
            seen_roots.add(source_root)
            sources.append((source, source_id))
        return sources

    async def _enumerate_all_sources(self, sources: list[tuple[SourceConfig, str]] | None = None) -> _EnumerationResult:
        """Enumerate sources while retaining which sources completed successfully."""
        sources = sources if sources is not None else self._validated_sources()
        artifacts: list[dict] = []
        successful_source_ids: set[str] = set()
        errors: dict[str, str] = {}
        blob_sources: list[SourceConfig] = []

        for source, source_id in sources:
            if source.source_type == "local":
                if source.discovery is DiscoveryMode.MANIFEST:
                    source_artifacts, error = self._enumerate_local_manifest(source)
                else:
                    source_artifacts, error = self._enumerate_local(source)
                artifacts.extend(source_artifacts)
                if error is None:
                    successful_source_ids.add(source_id)
                else:
                    errors[source_id] = error
            elif source.source_type == "blob":
                blob_sources.append(source)

        if blob_sources:
            try:
                blob_result = await self._enumerate_blob_sources_concurrent(blob_sources)
            except Exception as exc:
                error = _safe_source_error(exc)
                LOGGER.error("Failed to initialize Blob source enumeration: %s", type(exc).__name__)
                blob_result = _EnumerationResult(
                    [],
                    set(),
                    {_source_id(source): error for source in blob_sources},
                )
            artifacts.extend(blob_result.artifacts)
            successful_source_ids.update(blob_result.successful_source_ids)
            errors.update(blob_result.errors)

        return _EnumerationResult(artifacts, successful_source_ids, errors)

    def _isolate_manifest_candidate_conflicts(
        self,
        enumeration: _EnumerationResult,
        sources: list[tuple[SourceConfig, str]],
    ) -> _EnumerationResult:
        """Remove conflicting manifest sources before the shared refresh transaction."""
        manifest_sources = {
            source_id
            for source, source_id in sources
            if source.discovery is DiscoveryMode.MANIFEST and source_id in enumeration.successful_source_ids
        }
        candidate_owners: dict[str, set[str]] = {}
        for artifact in enumeration.artifacts:
            source_id = artifact["source_id"]
            if source_id in manifest_sources:
                candidate_owners.setdefault(artifact["artifact_id"], set()).add(source_id)

        failed_sources: set[str] = set()
        conflicting_candidate_ids: set[str] = set()
        for candidate_id, owners in candidate_owners.items():
            if len(owners) > 1:
                failed_sources.update(owners)
                conflicting_candidate_ids.add(candidate_id)

        candidate_ids = set(candidate_owners) - conflicting_candidate_ids
        for artifact in enumeration.artifacts:
            source_id = artifact["source_id"]
            if source_id not in manifest_sources or source_id in failed_sources:
                continue
            artifact_id = artifact["artifact_id"]
            for alias_value in artifact.get("aliases", []):
                namespace, alias = split_alias(alias_value)
                if any(
                    candidate_id != artifact_id and candidate_id in candidate_ids
                    for candidate_id in _alias_canonical_candidates(namespace, alias)
                ):
                    failed_sources.add(source_id)
                    break

        if not failed_sources:
            return enumeration

        errors = dict(enumeration.errors)
        errors.update({source_id: "manifest_invalid: source refresh failed" for source_id in failed_sources})
        LOGGER.error(
            "Rejected manifest identity conflicts for source(s): %s",
            ", ".join(sorted(failed_sources)),
        )
        return _EnumerationResult(
            [artifact for artifact in enumeration.artifacts if artifact["source_id"] not in failed_sources],
            enumeration.successful_source_ids - failed_sources,
            errors,
        )

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
                if source.discovery is DiscoveryMode.MANIFEST:
                    return await self._enumerate_blob_manifest(source, credential, clients)
                return await self._enumerate_blob_source(source, credential, clients)

        try:
            tasks = [enumerate_one(source) for source in sources]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            artifacts: list[dict] = []
            successful_source_ids: set[str] = set()
            errors: dict[str, str] = {}
            for source, result in zip(sources, results):
                source_id = _source_id(source)
                if isinstance(result, BaseException):
                    error = _safe_source_error(result)
                    LOGGER.error(
                        "Failed to enumerate blob source '%s': %s",
                        sanitize_uri_for_display(source.path),
                        type(result).__name__,
                    )
                    errors[source_id] = error
                else:
                    artifacts.extend(result)
                    successful_source_ids.add(source_id)
            return _EnumerationResult(artifacts, successful_source_ids, errors)
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
        source_root = canonicalize_azure_uri(source.path)
        service_url = f"https://{account}.blob.core.windows.net"

        # Reuse BlobServiceClient per account
        if service_url not in clients:
            clients[service_url] = BlobServiceClient(service_url, credential=credential)
        client = clients[service_url]

        artifacts: list[dict] = []
        now = datetime.now(timezone.utc).isoformat()

        container_client = client.get_container_client(container)
        prefix_boundary = prefix if prefix.endswith("/") else f"{prefix}/"
        async for blob in container_client.list_blobs(name_starts_with=prefix):
            if prefix and blob.name != prefix and not blob.name.startswith(prefix_boundary):
                continue
            if blob.name.endswith("/"):
                continue
            filename = blob.name.split("/")[-1]
            if filename.startswith("."):
                continue

            storage_uri = azure_uri_from_blob_name(account, container, blob.name)
            relative_name = (
                filename
                if prefix and blob.name == prefix
                else blob.name[len(prefix) :].lstrip("/")
                if prefix
                else blob.name
            )
            if is_scan_excluded_path(relative_name):
                continue
            logical_path = normalize_logical_path(relative_name)

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

    @staticmethod
    def _parse_manifest(payload: bytes, location: str) -> CatalogManifest:
        if len(payload) > MAX_MANIFEST_BYTES:
            raise ValueError(f"Manifest exceeds the {MAX_MANIFEST_BYTES}-byte size limit at {location}")
        try:
            raw = json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_json_keys,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise ValueError(f"Malformed manifest at {location}") from exc
        except ValueError as exc:
            raise ValueError(f"Invalid manifest at {location}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"Manifest at {location} must contain an object")
        try:
            return CatalogManifest.from_mapping(raw)
        except Exception as exc:
            raise ValueError(f"Invalid manifest at {location}: {exc}") from exc

    @staticmethod
    def _read_local_manifest(manifest_path: Path) -> bytes:
        try:
            declared_size = manifest_path.stat().st_size
        except OSError:
            raise
        if declared_size > MAX_MANIFEST_BYTES:
            raise ValueError(f"Manifest exceeds the {MAX_MANIFEST_BYTES}-byte size limit")
        with manifest_path.open("rb") as manifest_file:
            payload = manifest_file.read(MAX_MANIFEST_BYTES + 1)
        if len(payload) > MAX_MANIFEST_BYTES:
            raise ValueError(f"Manifest exceeds the {MAX_MANIFEST_BYTES}-byte size limit")
        return payload

    @staticmethod
    async def _read_blob_manifest(download: object) -> bytes:
        properties = getattr(download, "properties", None)
        declared_size = getattr(properties, "size", None)
        if declared_size is None and isinstance(properties, Mapping):
            declared_size = properties.get("size")
        if isinstance(declared_size, int) and declared_size > MAX_MANIFEST_BYTES:
            raise ValueError(f"Manifest exceeds the {MAX_MANIFEST_BYTES}-byte size limit")

        chunks_method = getattr(download, "chunks", None)
        if not callable(chunks_method):
            raise ValueError("Blob manifest download does not expose bounded chunks")
        payload = bytearray()
        async for chunk in chunks_method():
            remaining = MAX_MANIFEST_BYTES + 1 - len(payload)
            if remaining <= 0:
                break
            payload.extend(chunk[:remaining])
            if len(payload) > MAX_MANIFEST_BYTES:
                raise ValueError(f"Manifest exceeds the {MAX_MANIFEST_BYTES}-byte size limit")
        return bytes(payload)

    @staticmethod
    def _local_manifest_path(source: SourceConfig, source_root: Path) -> Path:
        manifest = Path(source.manifest or "")
        candidate = manifest.resolve() if manifest.is_absolute() else (source_root / manifest).resolve()
        try:
            candidate.relative_to(source_root)
        except ValueError as exc:
            raise ValueError("Local manifest must be within the configured source root") from exc
        return candidate

    def _enumerate_local_manifest(self, source: SourceConfig) -> tuple[list[dict], str | None]:
        source_root = Path(source.path).resolve()
        source_id = _source_id(source)
        try:
            if not source_root.is_dir():
                raise FileNotFoundError("source root does not exist or is not a directory")
            manifest_path = self._local_manifest_path(source, source_root)
            payload = self._read_local_manifest(manifest_path)
            manifest = self._parse_manifest(payload, str(manifest_path))
            manifest_etag = hashlib.sha256(payload).hexdigest()
            self._record_manifest_revision(source_id, manifest.generation, manifest_etag)
            now = datetime.now(timezone.utc).isoformat()
            artifacts = []
            for entry in manifest.artifacts:
                if is_reserved_provider_path(entry.path):
                    raise ValueError(f"Manifest artifact uses a reserved provider path: {entry.path}")
                storage_path = (source_root / entry.path).resolve()
                try:
                    storage_path.relative_to(source_root)
                except ValueError as exc:
                    raise ValueError(f"Manifest artifact escapes the configured source root: {entry.path}") from exc
                artifacts.append(
                    self._make_manifest_artifact(
                        entry,
                        source,
                        source_id,
                        str(source_root),
                        str(storage_path),
                        f"manifest-generation:{manifest.generation}",
                        now,
                    )
                )
            self._validate_manifest_artifacts(source_id, artifacts)
            return artifacts, None
        except Exception as exc:
            error = _safe_source_error(exc)
            LOGGER.error(
                "Failed to load local manifest for source %r: %s: %s",
                source_id,
                type(exc).__name__,
                exc,
            )
            return [], error

    @staticmethod
    def _blob_manifest_name(source: SourceConfig) -> tuple[str, str, str]:
        account, container, prefix = _parse_blob_path(source.path)
        manifest_value = source.manifest or ""
        decoded_segments = unquote(manifest_value).replace("\\", "/").split("/")
        if any(segment in {".", ".."} for segment in decoded_segments):
            raise ValueError("Blob manifest path must not contain dot segments")
        if "://" in manifest_value:
            manifest_account, manifest_container, manifest_name = _parse_blob_path(manifest_value)
            if (manifest_account, manifest_container) != (account, container):
                raise ValueError("Blob manifest must use the configured source account and container")
        else:
            manifest_name = "/".join(part for part in (prefix.rstrip("/"), manifest_value.lstrip("/")) if part)
        prefix_boundary = prefix.rstrip("/")
        if prefix_boundary and manifest_name != prefix_boundary and not manifest_name.startswith(f"{prefix_boundary}/"):
            raise ValueError("Blob manifest must be within the configured source prefix")
        return account, container, manifest_name

    async def _enumerate_blob_manifest(
        self,
        source: SourceConfig,
        credential: AsyncTokenCredential,
        clients: dict,
    ) -> list[dict]:
        from azure.storage.blob.aio import BlobServiceClient

        account, container, manifest_name = self._blob_manifest_name(source)
        source_id = _source_id(source)
        source_root = canonicalize_azure_uri(source.path)
        service_url = f"https://{account}.blob.core.windows.net"
        if service_url not in clients:
            clients[service_url] = BlobServiceClient(service_url, credential=credential)
        client = clients[service_url]
        blob_client = client.get_blob_client(container=container, blob=manifest_name)
        download = await blob_client.download_blob(
            offset=0,
            length=MAX_MANIFEST_BYTES + 1,
        )
        payload = await self._read_blob_manifest(download)
        manifest = self._parse_manifest(payload, azure_uri_from_blob_name(account, container, manifest_name))
        download_properties = getattr(download, "properties", None)
        response_etag = getattr(download_properties, "etag", None)
        if response_etag is None and isinstance(download_properties, Mapping):
            response_etag = download_properties.get("etag")
        manifest_etag = str(response_etag) if response_etag else f"sha256:{hashlib.sha256(payload).hexdigest()}"
        self._record_manifest_revision(source_id, manifest.generation, manifest_etag)
        _, _, prefix = _parse_blob_path(source.path)
        prefix_root = prefix.rstrip("/")
        now = datetime.now(timezone.utc).isoformat()
        artifacts = [
            self._make_manifest_artifact(
                entry,
                source,
                source_id,
                source_root,
                azure_uri_from_blob_name(
                    account,
                    container,
                    "/".join(part for part in (prefix_root, entry.path) if part),
                ),
                f"manifest-generation:{manifest.generation}",
                now,
            )
            for entry in manifest.artifacts
        ]
        self._validate_manifest_artifacts(source_id, artifacts)
        return artifacts

    def _validate_manifest_artifacts(self, source_id: str, artifacts: list[dict]) -> None:
        """Validate manifest identities against the source and retained catalog state."""
        artifact_paths: dict[str, str] = {}
        storage_paths: dict[str, str] = {}
        aliases: dict[tuple[str, str], tuple[str, str]] = {}
        existing_by_path = self._db.records_by_source_path(source_id, include_deleted=True)
        retained_ids_by_path = self._db.retained_artifact_ids_by_source_path(source_id)
        for artifact in artifacts:
            artifact_id = artifact["artifact_id"]
            logical_path = artifact["logical_path"]
            storage_uri = artifact["storage_uri"]
            previous_path = artifact_paths.get(artifact_id)
            if previous_path is not None:
                raise ValueError(
                    f"Manifest source {source_id!r} assigns artifact_id {artifact_id!r} "
                    f"to both {previous_path!r} and {logical_path!r}"
                )
            artifact_paths[artifact_id] = logical_path
            existing_by_id = self._db.get_artifact(
                artifact_id,
                include_deleted=True,
            )
            if existing_by_id is not None and existing_by_id.source_id != source_id:
                raise ValueError(
                    f"Manifest source {source_id!r} uses artifact_id {artifact_id!r} owned by another source"
                )
            existing_at_path = existing_by_path.get(logical_path)
            if existing_at_path is not None and existing_at_path.id != artifact_id:
                raise ValueError(
                    f"Manifest source {source_id!r} assigns {logical_path!r} to "
                    f"artifact_id {artifact_id!r}, but that path belongs to another artifact"
                )
            retained_ids = retained_ids_by_path.get(logical_path, set())
            if retained_ids - {artifact_id}:
                raise ValueError(
                    f"Manifest source {source_id!r} assigns retained path {logical_path!r} to "
                    f"artifact_id {artifact_id!r}, but that path belongs to another artifact"
                )
            previous_locator = storage_paths.get(storage_uri)
            if previous_locator is not None:
                raise ValueError(
                    f"Manifest source {source_id!r} maps both {previous_locator!r} "
                    f"and {logical_path!r} to the same storage locator"
                )
            storage_paths[storage_uri] = logical_path

            identity_alias = split_alias(_configured_id_alias(artifact_id))
            previous_identity = aliases.get(identity_alias)
            if previous_identity is not None and previous_identity != (artifact_id, logical_path):
                raise ValueError(
                    f"Manifest source {source_id!r} uses artifact_id {artifact_id!r} as an alias for another artifact"
                )
            aliases[identity_alias] = (artifact_id, logical_path)
            for alias_value in artifact.get("aliases", []):
                alias = split_alias(alias_value)
                for candidate_id in _alias_canonical_candidates(*alias):
                    existing_candidate = self._db.get_artifact(candidate_id, include_deleted=True)
                    if existing_candidate is not None and existing_candidate.id != artifact_id:
                        raise ValueError(
                            f"Manifest source {source_id!r} assigns alias "
                            f"{alias[0]}:{alias[1]} that collides with a canonical artifact ID"
                        )
                existing_alias_id = self._db.resolve_artifact_id(alias_value, source_id)
                if existing_alias_id is not None and existing_alias_id != artifact_id:
                    raise ValueError(
                        f"Manifest source {source_id!r} assigns alias "
                        f"{alias[0]}:{alias[1]} to a different retained artifact"
                    )
                previous = aliases.get(alias)
                if previous is not None:
                    previous_id, previous_alias_path = previous
                    if previous_id == artifact_id and previous_alias_path == logical_path:
                        continue
                    raise ValueError(
                        f"Manifest source {source_id!r} assigns alias {alias[0]}:{alias[1]} to multiple artifacts"
                    )
                aliases[alias] = (artifact_id, logical_path)

    def _make_manifest_artifact(
        self,
        entry: ManifestArtifact,
        source: SourceConfig,
        source_id: str,
        source_root: str,
        storage_uri: str,
        manifest_revision: str,
        indexed_at: str,
    ) -> dict:
        filename = entry.name or entry.path.rsplit("/", 1)[-1]
        override = None
        if source.files:
            override = source.files.get(entry.path) or source.files.get(filename)
        description = entry.description if entry.description is not None else source.description
        domain = entry.domain if entry.domain is not None else source.domain
        custom_id = entry.artifact_id
        aliases = list(entry.aliases)
        if override is not None:
            if override.description is not None:
                description = override.description
            if override.domain is not None:
                domain = override.domain
            if override.artifact_id is not None:
                custom_id = override.artifact_id
            aliases.extend(override.aliases)
        artifact_id = (
            (self._db.resolve_scan_alias(custom_id, source_id) if custom_id else None)
            or custom_id
            or logical_artifact_id(source_id, entry.path)
        )
        content_revision = entry.content_revision or entry.checksum_sha256 or manifest_revision
        metadata_revision = entry.metadata_revision or _revision_digest(
            [
                filename,
                description,
                domain,
                entry.media_type,
                entry.size_bytes,
                custom_id,
                aliases,
            ]
        )
        return {
            "artifact_id": artifact_id,
            "source_id": source_id,
            "logical_path": entry.path,
            "source_root": source_root,
            "name": filename,
            "storage_uri": storage_uri,
            "description": description,
            "domain": domain,
            "source_type": source.source_type,
            "content_type": entry.media_type or _infer_content_type(filename),
            "size_bytes": entry.size_bytes,
            "indexed_at": indexed_at,
            "content_revision": content_revision,
            "metadata_revision": metadata_revision,
            "checksum_sha256": entry.checksum_sha256,
            "aliases": [
                f"storage-uri:{storage_uri}",
                *([_configured_id_alias(custom_id)] if custom_id else []),
                *aliases,
            ],
            "_allow_move": True,
        }

    def _enumerate_local(self, source: SourceConfig) -> tuple[list[dict], str | None]:
        """Walk a local directory and produce artifact records."""
        source_path = Path(source.path).resolve()
        source_id = _source_id(source)
        if not source_path.exists():
            LOGGER.warning("Source path does not exist: %s", source_path)
            return [], "FileNotFoundError: source path does not exist"

        artifacts: list[dict] = []
        now = datetime.now(timezone.utc).isoformat()
        errors: list[OSError] = []
        try:
            if source_path.is_file():
                file_stat = _stat_regular_file_no_follow(source_path)
                artifacts.append(
                    self._make_local_artifact(
                        source_path,
                        source_path.name,
                        source_path.parent,
                        source_id,
                        source,
                        now,
                        file_stat,
                    )
                )
            else:
                root_fd = _open_directory_no_follow(source_path)
                try:
                    self._walk_local_directory(
                        root_fd,
                        (),
                        source_path,
                        source_id,
                        source,
                        now,
                        artifacts,
                        errors,
                    )
                finally:
                    os.close(root_fd)
        except OSError as exc:
            errors.append(exc)
        if errors:
            error = f"{type(errors[0]).__name__}: source enumeration failed"
            LOGGER.error("Failed to enumerate local source '%s': %s", source_path, error)
            return [], error
        return artifacts, None

    def _walk_local_directory(
        self,
        directory_fd: int,
        relative_parts: tuple[str, ...],
        source_path: Path,
        source_id: str,
        source: SourceConfig,
        indexed_at: str,
        artifacts: list[dict],
        errors: list[OSError],
    ) -> None:
        """Walk a retained directory descriptor without following swapped symlinks."""
        try:
            names = os.listdir(directory_fd)
        except OSError as exc:
            errors.append(exc)
            return
        for name in names:
            child_parts = (*relative_parts, name)
            relative_path = "/".join(child_parts)
            if is_scan_excluded_path(relative_path):
                continue
            try:
                entry_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if stat.S_ISDIR(entry_stat.st_mode):
                    child_fd = os.open(
                        name,
                        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=directory_fd,
                    )
                    try:
                        self._walk_local_directory(
                            child_fd,
                            child_parts,
                            source_path,
                            source_id,
                            source,
                            indexed_at,
                            artifacts,
                            errors,
                        )
                    finally:
                        os.close(child_fd)
                elif stat.S_ISREG(entry_stat.st_mode):
                    file_stat = _stat_regular_file_no_follow(name, dir_fd=directory_fd)
                    filepath = source_path.joinpath(*child_parts)
                    artifacts.append(
                        self._make_local_artifact(
                            filepath,
                            name,
                            source_path,
                            source_id,
                            source,
                            indexed_at,
                            file_stat,
                        )
                    )
            except OSError:
                continue

    def _make_local_artifact(
        self,
        filepath: Path,
        filename: str,
        source_root: Path,
        source_id: str,
        source: SourceConfig,
        indexed_at: str,
        file_stat: os.stat_result,
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
            "size_bytes": file_stat.st_size,
            "indexed_at": indexed_at,
            "content_revision": _revision_digest([file_stat.st_size, file_stat.st_mtime_ns]),
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

    async def _compute_rows(self, artifacts: list[tuple[dict, bool]]) -> list[dict]:
        """Compute only required embeddings before the atomic database refresh."""
        provider = self.embedding_provider
        reembed_artifacts = [artifact for artifact, reembed in artifacts if reembed]
        if provider is None:
            embeddings: list[Optional[list[float]]] = [None] * len(reembed_artifacts)
        else:
            texts = [_build_indexable_text(a["name"], a.get("description"), a.get("domain")) for a in reembed_artifacts]
            embeddings = []
            resolved_dimensions = self._db.vec_dimensions or provider.dimensions
            for i in range(0, len(texts), _EMBEDDING_BATCH_SIZE):
                batch = texts[i : i + _EMBEDDING_BATCH_SIZE]
                batch_embeddings = await provider.embed(batch)
                if len(batch_embeddings) != len(batch):
                    raise ValueError(
                        f"Embedding provider returned {len(batch_embeddings)} vectors for a batch of {len(batch)} texts."
                    )
                for embedding in batch_embeddings:
                    if resolved_dimensions is None:
                        resolved_dimensions = len(embedding)
                    if len(embedding) != resolved_dimensions:
                        raise ValueError(
                            "Embedding provider dimension mismatch: "
                            f"CatalogDB expects {resolved_dimensions}, got {len(embedding)}. "
                            "Construct CatalogDB with vec_dimensions=config.search.embedding_dimensions."
                        )
                embeddings.extend(batch_embeddings)
            if resolved_dimensions is not None:
                self._db.validate_vector_state(_embedding_model_id(self._config), resolved_dimensions)

        rows = []
        embedding_index = 0
        for artifact, reembed in artifacts:
            row = {**artifact, "_replace_embedding": reembed}
            if reembed:
                row["embedding"] = embeddings[embedding_index]
                embedding_index += 1
            rows.append(row)
        return rows
