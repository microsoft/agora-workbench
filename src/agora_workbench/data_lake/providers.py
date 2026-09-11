"""Public SQLite and manifest-backed catalog adapters."""

from __future__ import annotations

import asyncio
import base64
import json
from math import isfinite
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from agora_workbench.code_execution.data_access.catalog.config import CatalogConfig, DiscoveryMode
from agora_workbench.code_execution.data_access.catalog.db import ArtifactRecord, CatalogDB, SourceRefreshState
from agora_workbench.code_execution.data_access.catalog.indexer import CatalogIndexer, ManifestRefreshError

from .errors import ArtifactNotFoundError, BackendUnavailableError, InvalidRequestError
from .models import (
    READ_OPERATIONS,
    ArtifactPresentation,
    ArtifactReference,
    CatalogArtifact,
    ListRequest,
    Page,
    RequestContext,
    ResolvedArtifact,
    SearchRequest,
    SourceCapabilities,
    StorageLocator,
)
from .protocols import CatalogProvider

if TYPE_CHECKING:
    from agora_workbench.code_execution.auth import CredentialProvider

_MAX_CATALOG_RESULT_OFFSET = 10_000


@dataclass(frozen=True)
class CatalogReadiness:
    """Observable readiness and retained-refresh state."""

    ready: bool
    stale: bool
    sources: tuple[SourceRefreshState, ...]
    reason: str | None = None


def _safe_refresh_error(exc: BaseException) -> str:
    if isinstance(exc, ManifestRefreshError):
        details = "; ".join(f"{source_id}: {exc.errors[source_id]}" for source_id in exc.source_ids)
        return f"Manifest refresh failed: {details}"
    return f"Catalog refresh failed ({type(exc).__name__})"


def _parse_success_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _filters(filters: object) -> tuple[str | None, str | None]:
    if not hasattr(filters, "items"):
        raise InvalidRequestError("Catalog filters must be a mapping.", operation="catalog")
    values = dict(filters.items())  # type: ignore[union-attr]
    unknown = set(values) - {"domain", "source_type"}
    if unknown:
        raise InvalidRequestError(
            f"Unsupported catalog filters: {', '.join(sorted(unknown))}",
            operation="catalog",
        )
    domain = values.get("domain")
    source_type = values.get("source_type")
    if domain is not None and not isinstance(domain, str):
        raise InvalidRequestError("domain filter must be a string.", operation="catalog")
    if source_type is not None and not isinstance(source_type, str):
        raise InvalidRequestError("source_type filter must be a string.", operation="catalog")
    return domain, source_type


def _cursor_offset(cursor: str | None, expected: dict[str, object]) -> int:
    if cursor is None:
        return 0
    try:
        decoded = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
    except Exception as exc:
        raise InvalidRequestError("Catalog cursor is invalid.", operation="pagination") from exc
    if not isinstance(decoded, dict) or decoded.get("request") != expected:
        raise InvalidRequestError("Catalog cursor does not match the request.", operation="pagination")
    offset = decoded.get("offset")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0 or offset > _MAX_CATALOG_RESULT_OFFSET:
        raise InvalidRequestError("Catalog cursor offset is invalid.", operation="pagination")
    return offset


def _next_cursor(offset: int, returned: int, limit: int, request: dict[str, object]) -> str | None:
    if returned < limit:
        return None
    next_offset = offset + returned
    if next_offset > _MAX_CATALOG_RESULT_OFFSET:
        return None
    payload = json.dumps({"offset": next_offset, "request": request}, sort_keys=True, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode()


def _artifact(record: ArtifactRecord, *, requested_reference: ArtifactReference | None = None) -> CatalogArtifact:
    return CatalogArtifact(
        reference=requested_reference or ArtifactReference(record.id, source_id=record.source_id),
        presentation=ArtifactPresentation(
            record.name,
            description=record.description,
            media_type=record.content_type,
            size_bytes=record.size_bytes,
        ),
        locator=StorageLocator(record.storage_uri),
        metadata={
            key: value
            for key, value in {
                "logical_path": record.logical_path,
                "domain": record.domain,
                "source_type": record.source_type,
                "indexed_at": record.indexed_at,
            }.items()
            if value is not None
        },
        revision=record.current_revision,
        content_revision=record.content_revision,
        metadata_revision=record.metadata_revision,
        checksum_sha256=record.checksum_sha256,
        deleted_at=datetime.fromisoformat(record.deleted_at) if record.deleted_at else None,
    )


class SQLiteCatalogProvider:
    """CatalogProvider adapter over an opened, caller-owned CatalogDB."""

    def __init__(self, db: CatalogDB, source_ids: tuple[str, ...]):
        self._db = db
        self._source_ids = tuple(dict.fromkeys(source_ids))
        if not self._source_ids or any(not source_id for source_id in self._source_ids):
            raise ValueError("SQLiteCatalogProvider requires at least one non-empty source_id")

    async def capabilities(self) -> tuple[SourceCapabilities, ...]:
        return tuple(SourceCapabilities(source_id, READ_OPERATIONS) for source_id in self._source_ids)

    def _effective_source_ids(self, requested: tuple[str, ...]) -> tuple[str, ...]:
        if not requested:
            return self._source_ids
        configured = set(self._source_ids)
        return tuple(source_id for source_id in requested if source_id in configured)

    @staticmethod
    def _validate_page_limit(limit: int) -> None:
        if limit > 100:
            raise InvalidRequestError("SQLite catalog pages are limited to 100 artifacts.", operation="pagination")

    async def search(self, request: SearchRequest, context: RequestContext) -> Page[CatalogArtifact]:
        del context
        self._validate_page_limit(request.page.limit)
        domain, source_type = _filters(request.filters)
        source_ids = self._effective_source_ids(request.source_ids)
        if not source_ids:
            return Page(())
        cursor_request = {
            "operation": "search",
            "query": request.query,
            "source_ids": list(request.source_ids),
            "filters": dict(request.filters),
        }
        offset = _cursor_offset(request.page.cursor, cursor_request)
        records = self._db.search(
            request.query,
            domain=domain,
            source_type=source_type,
            source_ids=source_ids,
            top=request.page.limit + 1,
            offset=offset,
        )
        page_records = records[: request.page.limit]
        has_more = len(records) > request.page.limit
        return Page(
            tuple(_artifact(record) for record in page_records),
            _next_cursor(offset, len(page_records), request.page.limit, cursor_request) if has_more else None,
        )

    async def list(self, request: ListRequest, context: RequestContext) -> Page[CatalogArtifact]:
        del context
        self._validate_page_limit(request.page.limit)
        domain, source_type = _filters(request.filters)
        source_ids = self._effective_source_ids(request.source_ids)
        if not source_ids:
            return Page(())
        cursor_request = {
            "operation": "list",
            "source_ids": list(request.source_ids),
            "filters": dict(request.filters),
        }
        offset = _cursor_offset(request.page.cursor, cursor_request)
        records = self._db.list_artifacts(
            source_ids=source_ids,
            domain=domain,
            source_type=source_type,
            limit=request.page.limit + 1,
            offset=offset,
        )
        page_records = records[: request.page.limit]
        cursor = (
            _next_cursor(offset, len(page_records), request.page.limit, cursor_request)
            if len(records) > request.page.limit
            else None
        )
        return Page(tuple(_artifact(record) for record in page_records), cursor)

    async def get(self, reference: ArtifactReference, context: RequestContext) -> CatalogArtifact:
        del context
        if reference.source_id not in self._source_ids:
            raise ArtifactNotFoundError(
                "Catalog artifact was not found.",
                resource_id=reference.artifact_id,
                operation="get",
            )
        record = self._db.get_artifact(
            reference.artifact_id,
            source_id=reference.source_id,
            revision=reference.revision,
        )
        if record is None:
            raise ArtifactNotFoundError(
                "Catalog artifact was not found.",
                resource_id=reference.artifact_id,
                operation="get",
            )
        return _artifact(record, requested_reference=reference)

    async def resolve(self, reference: ArtifactReference, context: RequestContext) -> ResolvedArtifact:
        artifact = await self.get(reference, context)
        if artifact.locator is None:
            raise ArtifactNotFoundError(
                "Catalog artifact has no storage locator.",
                resource_id=reference.artifact_id,
                operation="resolve",
            )
        return ResolvedArtifact(reference=artifact.reference, locator=artifact.locator)


class ManifestCatalogProvider(SQLiteCatalogProvider):
    """Authoritative manifest catalog with a private rebuildable SQLite index."""

    def __init__(
        self,
        config: CatalogConfig,
        *,
        db_path: str | Path = ":memory:",
        credential_provider: CredentialProvider | None = None,
        max_stale_seconds: float = 300.0,
    ):
        if not config.sources:
            raise ValueError("ManifestCatalogProvider requires at least one source")
        if any(source.discovery is not DiscoveryMode.MANIFEST for source in config.sources):
            raise ValueError("ManifestCatalogProvider accepts only discovery='manifest' sources")
        if max_stale_seconds < 0:
            raise ValueError("max_stale_seconds must be non-negative")
        if not isfinite(max_stale_seconds):
            raise ValueError("max_stale_seconds must be finite")
        self._config = config
        self._closed = False
        self._lifecycle_lock = asyncio.Lock()
        self._last_states: tuple[SourceRefreshState, ...] = ()
        self._db_owned = CatalogDB(db_path, vec_dimensions=config.search.embedding_dimensions)
        try:
            self._db_owned.open()
            self._indexer = CatalogIndexer(config, self._db_owned, credential_provider=credential_provider)
            self._source_stale_limits = {
                source.source_id or "": (
                    source.max_stale_seconds if source.max_stale_seconds is not None else max_stale_seconds
                )
                for source in config.sources
            }
            self._last_error: str | None = None
            self._load_attempted = False
            super().__init__(self._db_owned, tuple(source.source_id or "" for source in config.sources))
        except BaseException:
            self._db_owned.close()
            self._closed = True
            raise

    def _current_source_states(self) -> tuple[SourceRefreshState, ...]:
        if self._closed:
            return self._last_states
        expected = set(self._source_ids)
        states = tuple(state for state in self._db_owned.list_source_refresh_states() if state.source_id in expected)
        self._last_states = states
        return states

    def _has_successful_generation(self) -> bool:
        successful = {
            state.source_id
            for state in self._current_source_states()
            if state.successful_generation > 0 and state.manifest_generation is not None
        }
        return set(self._source_ids) <= successful

    async def load(self) -> int:
        """Load or refresh all manifests, retaining the previous valid generation on failure."""
        async with self._lifecycle_lock:
            return await self._load_unlocked()

    async def _load_unlocked(self) -> int:
        if self._closed:
            raise BackendUnavailableError("Manifest catalog is closed.", operation="refresh")
        self._load_attempted = True
        had_successful_generation = self._has_successful_generation()
        refresh_error: BackendUnavailableError | None = None
        try:
            count = await self._indexer.index()
        except asyncio.CancelledError:
            self._last_error = "Manifest catalog refresh was cancelled."
            self._current_source_states()
            if not had_successful_generation:
                self._close_unlocked()
            raise
        except Exception as exc:
            self._last_error = _safe_refresh_error(exc)
            self._current_source_states()
            if had_successful_generation or self._has_successful_generation():
                message = "Manifest catalog refresh failed; the last valid generation was preserved."
            else:
                message = "Manifest catalog refresh failed; no valid generation is available."
            refresh_error = BackendUnavailableError(f"{message} {self._last_error}", operation="refresh")
        else:
            self._last_error = None
            self._current_source_states()
            return count
        raise refresh_error

    def readiness(self) -> CatalogReadiness:
        """Return readiness, stale bounds, and per-source refresh state."""
        states = self._current_source_states()
        if self._closed:
            return CatalogReadiness(
                False,
                self._last_error is not None,
                states,
                self._last_error or "Manifest catalog is closed.",
            )
        if not self._load_attempted:
            return CatalogReadiness(False, False, states, "Manifest catalog load() has not completed.")
        expected = set(self._source_ids)
        successful = {
            state.source_id
            for state in states
            if state.successful_generation > 0 and state.manifest_generation is not None
        }
        if not expected <= successful:
            return CatalogReadiness(False, False, states, self._last_error or "No valid manifest generation loaded.")
        if self._last_error is None:
            return CatalogReadiness(True, False, states)
        now = datetime.now(timezone.utc)
        expired_sources: list[str] = []
        for state in states:
            if state.last_success_at is None:
                expired_sources.append(state.source_id)
                continue
            try:
                success_at = _parse_success_timestamp(state.last_success_at)
            except (TypeError, ValueError):
                expired_sources.append(state.source_id)
                continue
            age = max(0.0, (now - success_at).total_seconds())
            if age > self._source_stale_limits[state.source_id]:
                expired_sources.append(state.source_id)
        if expired_sources:
            return CatalogReadiness(
                False,
                True,
                states,
                "Stale catalog data expired after a refresh failure for source(s): "
                + ", ".join(sorted(expired_sources)),
            )
        return CatalogReadiness(True, True, states, "Serving the last valid generation after a refresh failure.")

    def _require_ready(self) -> None:
        readiness = self.readiness()
        if not readiness.ready:
            raise BackendUnavailableError(
                readiness.reason or "Manifest catalog is not ready.",
                operation="catalog",
            )

    async def capabilities(self) -> tuple[SourceCapabilities, ...]:
        self._require_ready()
        return await super().capabilities()

    async def search(self, request: SearchRequest, context: RequestContext) -> Page[CatalogArtifact]:
        self._require_ready()
        return await super().search(request, context)

    async def list(self, request: ListRequest, context: RequestContext) -> Page[CatalogArtifact]:
        self._require_ready()
        return await super().list(request, context)

    async def get(self, reference: ArtifactReference, context: RequestContext) -> CatalogArtifact:
        self._require_ready()
        return await super().get(reference, context)

    async def resolve(self, reference: ArtifactReference, context: RequestContext) -> ResolvedArtifact:
        self._require_ready()
        return await super().resolve(reference, context)

    async def aclose(self) -> None:
        """Close the private per-reader SQLite cache."""
        async with self._lifecycle_lock:
            self._close_unlocked()

    def _close_unlocked(self) -> None:
        if not self._closed:
            self._current_source_states()
            self._closed = True
            self._db_owned.close()

    async def __aenter__(self) -> "ManifestCatalogProvider":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object,
    ) -> None:
        await self.aclose()


class CatalogArtifactResolver:
    """Legacy ArtifactResolver adapter for one source of a CatalogProvider."""

    def __init__(
        self,
        provider: CatalogProvider,
        source_id: str,
        context: RequestContext | None = None,
    ):
        self._provider = provider
        self._source_id = source_id
        self._context = context or RequestContext()
        self._closed = False

    @property
    def unavailable_reason(self) -> str | None:
        if self._closed:
            return "The artifact resolver has been closed."
        if isinstance(self._provider, ManifestCatalogProvider):
            readiness = self._provider.readiness()
            return None if readiness.ready else readiness.reason
        return None

    async def resolve(self, artifact_id: str) -> str:
        if self._closed:
            raise ValueError(self.unavailable_reason)
        resolved = await self._provider.resolve(
            ArtifactReference(artifact_id, source_id=self._source_id),
            self._context,
        )
        return resolved.locator.uri

    async def aclose(self) -> None:
        self._closed = True


__all__ = [
    "CatalogArtifactResolver",
    "CatalogReadiness",
    "ManifestCatalogProvider",
    "SQLiteCatalogProvider",
]
