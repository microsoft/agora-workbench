"""Recoverable, optimistic-concurrency managed catalog writes.

Managed writes deliberately sit above asset transfer. Revision bytes are
created first at immutable paths, then a manifest generation is conditionally
committed. Those two steps are recoverable, but are not an atomic transaction.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from .errors import (
    ArtifactNotFoundError,
    ConflictError,
    InvalidRequestError,
    PermissionDeniedError,
    PreconditionFailedError,
    ReconciliationError,
    RetryExhaustedError,
    TransferTimeoutError,
    UnsafePathError,
    UnsupportedOperationError,
)
from .identity import (
    RESERVED_MANIFEST_PATH,
    RESERVED_OPERATIONS_PREFIX,
    RESERVED_PROVIDER_PREFIX,
    RESERVED_RECEIPTS_PREFIX,
    RESERVED_REVISIONS_PREFIX,
    is_reserved_provider_path,
    logical_artifact_id,
    normalize_logical_path,
    validate_managed_revision_path,
)
from .manifest import (
    MANIFEST_VERSION,
    MAX_MANIFEST_BYTES,
    CatalogManifest,
    ManifestArtifact,
    ManifestOwnership,
    ManifestProvenance,
    ManifestRemoval,
    ManifestRevision,
)
from .models import (
    WRITE_OPERATIONS,
    ArtifactReference,
    CatalogAuthorizationRequest,
    CatalogOperation,
    RequestContext,
    SourceCapabilities,
)
from .protocols import CatalogAuthorizer
from .transfer import (
    TransferDiagnostic,
    TransferOptions,
    await_transfer,
    check_transfer_cancelled,
    check_transfer_size,
    emit_transfer_diagnostic,
)

_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_STAGING_PREFIX = f"{RESERVED_PROVIDER_PREFIX}staging/"
_WRITER_LOCK_PATH = f"{RESERVED_PROVIDER_PREFIX}writer.lock"


def _operation_path(operation_id: str) -> str:
    return f"{RESERVED_OPERATIONS_PREFIX}{operation_id}.json"


def _managed_receipt_path(operation_id: str) -> str:
    return f"{RESERVED_RECEIPTS_PREFIX}{operation_id}.json"


def _revision_path(artifact_id: str, operation_id: str, suffix: str = "") -> str:
    return validate_managed_revision_path(f"{RESERVED_REVISIONS_PREFIX}{artifact_id}/{operation_id}{suffix}")


def _normalize_catalog_path(path: str) -> str:
    normalized = normalize_logical_path(path)
    if is_reserved_provider_path(normalized):
        raise UnsafePathError(
            "Artifact logical paths cannot use the reserved Agora namespace.",
            operation="managed_write",
        )
    return normalized


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Mapping[str, object]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _remaining_transfer_options(options: TransferOptions, started: float, *, operation: str) -> TransferOptions:
    if options.timeout_seconds is None:
        return options
    remaining = options.timeout_seconds - (time.monotonic() - started)
    if remaining <= 0:
        raise TransferTimeoutError(
            f"Transfer exceeded the configured {options.timeout_seconds:g}-second timeout.",
            operation=operation,
        )
    return replace(options, timeout_seconds=remaining)


def _commit_fence_generation(manifest: CatalogManifest, operation_id: str) -> int | None:
    prefix = f"{operation_id}:"
    for fence in manifest.commit_fences:
        if fence.startswith(prefix):
            try:
                return int(fence[len(prefix) :])
            except ValueError:
                return None
    return None


def _without_commit_fence(manifest: CatalogManifest, operation_id: str) -> tuple[str, ...]:
    prefix = f"{operation_id}:"
    return tuple(fence for fence in manifest.commit_fences if not fence.startswith(prefix))


def _fingerprint(value: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _validate_operation_id(operation_id: str) -> str:
    if not _OPERATION_ID.fullmatch(operation_id):
        raise InvalidRequestError(
            "Operation ID must be 1-128 URL-safe characters.",
            resource_id=operation_id,
            operation="managed_write",
        )
    return operation_id


@dataclass(frozen=True)
class ArtifactMetadata:
    """Metadata supplied for a durable artifact registration."""

    name: str | None = None
    description: str | None = None
    domain: str | None = None
    media_type: str | None = None
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "aliases", tuple(self.aliases))


@dataclass(frozen=True)
class RegisterArtifactRequest:
    """Snapshot caller-owned bytes into a durable immutable managed revision."""

    operation_id: str
    path: str
    storage_path: str
    artifact_id: str | None = None
    metadata: ArtifactMetadata = field(default_factory=ArtifactMetadata)
    content_revision: str | None = None
    checksum_sha256: str | None = None
    size_bytes: int | None = None
    expected_generation: int | None = None
    expected_revision_id: str | None = None
    transfer_options: TransferOptions = field(default_factory=TransferOptions)


@dataclass(frozen=True)
class UploadArtifactRequest:
    """Upload local bytes as a new immutable managed revision."""

    operation_id: str
    path: str
    local_path: Path
    artifact_id: str | None = None
    metadata: ArtifactMetadata = field(default_factory=ArtifactMetadata)
    expected_generation: int | None = None
    expected_revision_id: str | None = None
    transfer_options: TransferOptions = field(default_factory=TransferOptions)


@dataclass(frozen=True)
class PromoteOutputRequest(UploadArtifactRequest):
    """Explicitly promote a scratch/session output to a durable artifact."""

    session_id: str = ""
    output_name: str = ""


@dataclass(frozen=True)
class RemoveArtifactRequest:
    """Commit a tombstone and then optionally collect owned revision bytes."""

    operation_id: str
    reference: ArtifactReference
    expected_generation: int | None = None
    expected_revision_id: str | None = None
    garbage_collect: bool = True


@dataclass(frozen=True)
class ManagedWriteResult:
    """Committed write result and its read-after-write generation."""

    operation_id: str
    source_id: str
    artifact_id: str
    generation: int
    revision_id: str | None
    storage_path: str | None
    deleted: bool = False
    cleanup_pending: bool = False


@dataclass(frozen=True)
class ReconciliationReport:
    """Outcome of scanning interrupted operation intents."""

    recovered: tuple[str, ...] = ()
    removed_orphans: tuple[str, ...] = ()
    deferred: tuple[str, ...] = ()
    removed_staging: tuple[str, ...] = ()
    deferred_staging: tuple[str, ...] = ()
    failures: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "failures", MappingProxyType(dict(self.failures)))


@dataclass(frozen=True)
class _ObjectVersion:
    """Storage version returned after a create-exclusive object write."""

    token: str
    size_bytes: int
    operation_id: str | None = None
    checksum_sha256: str | None = None


class DeleteOutcome(StrEnum):
    """Ownership-aware deletion postcondition."""

    DELETED = "deleted"
    ABSENT = "absent"
    MISMATCH = "mismatch"


class _StorageConflict(Exception):
    pass


class _ManagedStorageBackend(Protocol):
    """Storage seam used by the write state machine.

    Transfer behavior stays behind this private adapter while operation records,
    manifest CAS, and reconciliation remain backend-neutral.
    """

    def serialized(self) -> AbstractAsyncContextManager[None]: ...

    async def read_json(self, path: str) -> tuple[dict[str, object] | None, str | None]: ...

    async def create_json(self, path: str, value: Mapping[str, object]) -> bool: ...

    async def replace_json(
        self,
        path: str,
        value: Mapping[str, object],
        expected_token: str | None,
    ) -> str: ...

    async def create_from_file(
        self,
        path: str,
        local_path: Path,
        operation_id: str,
        checksum_sha256: str | None,
        options: TransferOptions,
    ) -> _ObjectVersion: ...

    async def create_from_storage(
        self,
        source_path: str,
        target_path: str,
        operation_id: str,
        checksum_sha256: str,
        options: TransferOptions,
    ) -> _ObjectVersion: ...

    async def exists(self, path: str) -> _ObjectVersion | None: ...

    async def delete_owned(
        self,
        path: str,
        operation_id: str,
        version_token: str | None,
    ) -> DeleteOutcome: ...

    async def list_json(self, prefix: str) -> tuple[tuple[str, dict[str, object]], ...]: ...

    async def reconcile_staging(
        self,
        active_operation_ids: frozenset[str],
        grace_seconds: float,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]: ...


class LocalManagedStorage:
    """Create-exclusive local objects and atomically replaced manifests.

    Writers use an advisory ``flock`` covering the full mutation. Files and
    containing directories are fsynced before the lock is released. Readers do
    not take the lock; atomic replacement means they observe either the old or
    new complete manifest.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.root / _WRITER_LOCK_PATH

    def _path(self, relative: str) -> Path:
        normalized = normalize_logical_path(relative)
        result = (self.root / normalized).resolve()
        if not result.is_relative_to(self.root):
            raise InvalidRequestError("Managed path escapes the configured root.", operation="managed_write")
        return result

    @staticmethod
    def _token(path: Path) -> str:
        stat = path.stat()
        return f"{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}"

    @staticmethod
    def _checksum(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _owned_operation_id(path: Path) -> str | None:
        try:
            return os.getxattr(path, b"user.agora.operation_id").decode()
        except (AttributeError, OSError, UnicodeDecodeError):
            return None

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _mkdir_durable(self, path: Path) -> None:
        missing: list[Path] = []
        cursor = path
        while cursor != self.root and not cursor.exists():
            missing.append(cursor)
            cursor = cursor.parent
        for directory in reversed(missing):
            try:
                directory.mkdir()
            except FileExistsError:
                pass
            self._fsync_directory(directory.parent)

    @asynccontextmanager
    async def serialized(self) -> AsyncIterator[None]:
        import fcntl

        self._mkdir_durable(self._lock_path.parent)
        descriptor = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            await asyncio.to_thread(fcntl.flock, descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    async def read_json(self, path: str) -> tuple[dict[str, object] | None, str | None]:
        target = self._path(path)
        try:
            payload = await asyncio.to_thread(target.read_bytes)
        except FileNotFoundError:
            return None, None
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise InvalidRequestError("Managed JSON object must contain a mapping.", operation="managed_write")
        return value, self._token(target)

    async def create_json(self, path: str, value: Mapping[str, object]) -> bool:
        target = self._path(path)
        self._mkdir_durable(target.parent)
        try:
            with target.open("xb") as stream:
                stream.write(_canonical_json(value))
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            return False
        self._fsync_directory(target.parent)
        return True

    async def replace_json(
        self,
        path: str,
        value: Mapping[str, object],
        expected_token: str | None,
    ) -> str:
        target = self._path(path)
        self._mkdir_durable(target.parent)
        if target.exists():
            if expected_token is None or self._token(target) != expected_token:
                raise _StorageConflict
        elif expected_token is not None:
            raise _StorageConflict
        temporary = target.with_name(f".{target.name}.{os.getpid()}.{time.time_ns()}")
        try:
            with temporary.open("xb") as stream:
                stream.write(_canonical_json(value))
                stream.flush()
                os.fsync(stream.fileno())
            if expected_token is None and target.exists():
                raise _StorageConflict
            os.replace(temporary, target)
            self._fsync_directory(target.parent)
        finally:
            temporary.unlink(missing_ok=True)
        return self._token(target)

    async def create_from_file(
        self,
        path: str,
        local_path: Path,
        operation_id: str,
        checksum_sha256: str | None,
        options: TransferOptions,
    ) -> _ObjectVersion:
        if options.object_metadata:
            raise UnsupportedOperationError(
                "Local managed storage does not support caller object metadata.",
                operation="upload",
            )
        target = self._path(path)
        stage_dir = self._path(f"{_STAGING_PREFIX}{operation_id}")
        stage = stage_dir / f"{uuid.uuid4().hex}.stage"
        async with self.serialized():
            self._mkdir_durable(target.parent)
            self._mkdir_durable(stage_dir)
            with stage.open("xb") as staged:
                staged.flush()
                os.fsync(staged.fileno())
            self._fsync_directory(stage_dir)
        try:
            check_transfer_cancelled(options, operation="upload", resource=str(local_path))
            transferred_checksum = await asyncio.to_thread(self._copy_stage, local_path, stage, options)
            if options.expected_sha256 is not None and transferred_checksum != options.expected_sha256:
                raise PreconditionFailedError("Upload checksum did not match TransferOptions.", operation="upload")
            if checksum_sha256 is not None and transferred_checksum != checksum_sha256:
                raise PreconditionFailedError(
                    "Upload source changed while it was being transferred.",
                    operation="upload",
                )
            async with self.serialized():
                check_transfer_cancelled(options, operation="upload", resource=str(local_path))
                os.setxattr(stage, b"user.agora.operation_id", operation_id.encode())
                with stage.open("rb") as staged:
                    os.fsync(staged.fileno())
                try:
                    os.link(stage, target)
                except FileExistsError as exc:
                    raise _StorageConflict from exc
                self._fsync_directory(target.parent)
        finally:
            async with self.serialized():
                stage.unlink(missing_ok=True)
                self._fsync_directory(stage_dir)
                try:
                    stage_dir.rmdir()
                except OSError:
                    pass
                else:
                    self._fsync_directory(stage_dir.parent)
        return _ObjectVersion(
            self._token(target),
            target.stat().st_size,
            self._owned_operation_id(target),
            transferred_checksum,
        )

    @staticmethod
    def _copy_stage(local_path: Path, stage: Path, options: TransferOptions) -> str:
        digest = hashlib.sha256()
        transferred = 0
        started = time.monotonic()
        with local_path.open("rb") as source, stage.open("wb") as staged:
            for chunk in iter(lambda: source.read(options.chunk_size), b""):
                if options.timeout_seconds is not None and time.monotonic() - started > options.timeout_seconds:
                    raise TransferTimeoutError(
                        f"Transfer exceeded the configured {options.timeout_seconds:g}-second timeout.",
                        operation="upload",
                    )
                check_transfer_cancelled(options, operation="upload", resource=str(local_path))
                transferred += len(chunk)
                check_transfer_size(transferred, options, operation="upload", resource=str(local_path))
                staged.write(chunk)
                digest.update(chunk)
            staged.flush()
            os.fsync(staged.fileno())
        return digest.hexdigest()

    async def create_from_storage(
        self,
        source_path: str,
        target_path: str,
        operation_id: str,
        checksum_sha256: str,
        options: TransferOptions,
    ) -> _ObjectVersion:
        return await self.create_from_file(
            target_path,
            self._path(source_path),
            operation_id,
            checksum_sha256,
            options,
        )

    async def exists(self, path: str) -> _ObjectVersion | None:
        target = self._path(path)
        if not target.is_file():
            return None
        return _ObjectVersion(
            self._token(target),
            target.stat().st_size,
            self._owned_operation_id(target),
            self._checksum(target),
        )

    async def delete_owned(
        self,
        path: str,
        operation_id: str,
        version_token: str | None,
    ) -> DeleteOutcome:
        target = self._path(path)
        if not target.exists():
            return DeleteOutcome.ABSENT
        validate_managed_revision_path(path)
        if self._owned_operation_id(target) != operation_id:
            return DeleteOutcome.MISMATCH
        if version_token is not None and self._token(target) != version_token:
            return DeleteOutcome.MISMATCH
        target.unlink()
        self._fsync_directory(target.parent)
        return DeleteOutcome.DELETED

    async def list_json(self, prefix: str) -> tuple[tuple[str, dict[str, object]], ...]:
        root = self._path(prefix)
        if not root.exists():
            return ()
        values: list[tuple[str, dict[str, object]]] = []
        for path in root.rglob("*.json"):
            try:
                value = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                values.append((path.relative_to(self.root).as_posix(), value))
        return tuple(values)

    async def reconcile_staging(
        self,
        active_operation_ids: frozenset[str],
        grace_seconds: float,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        root = self._path(_STAGING_PREFIX.rstrip("/"))
        if not root.exists():
            return (), ()
        removed: list[str] = []
        deferred: list[str] = []
        now = time.time()
        for operation_dir in root.iterdir():
            if not operation_dir.is_dir():
                continue
            operation_id = operation_dir.name
            intent_path = self._path(_operation_path(operation_id))
            intent_active = False
            try:
                intent = json.loads(intent_path.read_text())
                lease_until = datetime.fromisoformat(str(intent.get("lease_until", "")).replace("Z", "+00:00"))
                intent_active = intent.get("state") in {"active", "commit_ready"} and lease_until > datetime.now(
                    timezone.utc
                )
            except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
                pass
            if operation_id in active_operation_ids or intent_active:
                deferred.append(operation_id)
                continue
            stages = tuple(operation_dir.glob("*.stage"))
            if any(now - stage.stat().st_mtime < grace_seconds for stage in stages):
                deferred.append(operation_id)
                continue
            for stage in stages:
                stage.unlink(missing_ok=True)
            self._fsync_directory(operation_dir)
            try:
                operation_dir.rmdir()
            except OSError:
                deferred.append(operation_id)
            else:
                self._fsync_directory(root)
                removed.append(operation_id)
        return tuple(removed), tuple(deferred)


class BlobManagedStorage:
    """Blob-container adapter using create-only objects and ETag manifest CAS.

    The caller owns the container client and its prefix. Transfers apply the
    shared safety options while this adapter retains lifecycle-specific
    ownership metadata and conditional state operations.
    """

    def __init__(self, container_client: object, *, prefix: str = "") -> None:
        self._container_client = container_client
        self._prefix = prefix.strip("/")

    def _path(self, path: str) -> str:
        normalized = normalize_logical_path(path)
        return "/".join(part for part in (self._prefix, normalized) if part)

    def _blob(self, path: str):
        return self._container_client.get_blob_client(self._path(path))  # type: ignore[attr-defined]

    @staticmethod
    def _missing(exc: BaseException) -> bool:
        return getattr(exc, "status_code", None) == 404 or type(exc).__name__ == "ResourceNotFoundError"

    @staticmethod
    def _conflict(exc: BaseException) -> bool:
        return getattr(exc, "status_code", None) in {409, 412} or type(exc).__name__ in {
            "ResourceExistsError",
            "ResourceModifiedError",
        }

    @staticmethod
    def _response_etag(response: object) -> str | None:
        etag = response.get("etag") if isinstance(response, Mapping) else getattr(response, "etag", None)
        return str(etag) if etag is not None else None

    async def _created_version(
        self,
        blob: object,
        path: str,
        operation_id: str,
        checksum_sha256: str,
        created_etag: str | None,
        options: TransferOptions,
        started: float,
        operation: str,
    ) -> _ObjectVersion:
        from azure.core import MatchConditions

        if created_etag is None:
            raise PreconditionFailedError(
                "Created object did not return a version token for safe verification.",
                operation=operation,
            )
        try:
            properties = await await_transfer(
                blob.get_blob_properties(  # type: ignore[attr-defined]
                    etag=created_etag,
                    match_condition=MatchConditions.IfNotModified,
                ),
                _remaining_transfer_options(options, started, operation=operation),
                operation=operation,
                resource=path,
            )
        except Exception as exc:
            if self._missing(exc) or self._conflict(exc):
                raise PreconditionFailedError(
                    "Created object changed before it could be verified.",
                    operation=operation,
                ) from exc
            raise
        current_etag = str(properties.etag)
        metadata = getattr(properties, "metadata", {}) or {}
        if current_etag != created_etag or metadata.get("agora_operation_id") != operation_id:
            raise PreconditionFailedError(
                "Created object identity or ownership metadata did not match the upload.",
                operation=operation,
            )
        return _ObjectVersion(current_etag, int(properties.size), operation_id, checksum_sha256)

    @asynccontextmanager
    async def serialized(self) -> AsyncIterator[None]:
        yield

    async def read_json(self, path: str) -> tuple[dict[str, object] | None, str | None]:
        try:
            download = await self._blob(path).download_blob()
            payload = await download.readall()
        except Exception as exc:
            if self._missing(exc):
                return None, None
            raise
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise InvalidRequestError("Managed JSON object must contain a mapping.", operation="managed_write")
        properties = getattr(download, "properties", None)
        etag = getattr(properties, "etag", None)
        return value, str(etag) if etag is not None else None

    async def create_json(self, path: str, value: Mapping[str, object]) -> bool:
        try:
            await self._blob(path).upload_blob(_canonical_json(value), overwrite=False)
        except Exception as exc:
            if self._conflict(exc):
                return False
            raise
        return True

    async def replace_json(
        self,
        path: str,
        value: Mapping[str, object],
        expected_token: str | None,
    ) -> str:
        from azure.core import MatchConditions

        blob = self._blob(path)
        try:
            if expected_token is None:
                response = await blob.upload_blob(_canonical_json(value), overwrite=False)
            else:
                response = await blob.upload_blob(
                    _canonical_json(value),
                    overwrite=True,
                    etag=expected_token,
                    match_condition=MatchConditions.IfNotModified,
                )
        except Exception as exc:
            if self._conflict(exc):
                raise _StorageConflict from exc
            raise
        etag = response.get("etag") if isinstance(response, Mapping) else getattr(response, "etag", None)
        if etag is None:
            properties = await blob.get_blob_properties()
            etag = properties.etag
        return str(etag)

    async def create_from_file(
        self,
        path: str,
        local_path: Path,
        operation_id: str,
        checksum_sha256: str | None,
        options: TransferOptions,
    ) -> _ObjectVersion:
        from azure.core import MatchConditions

        started = time.monotonic()
        digest = hashlib.sha256()
        try:

            async def chunks():
                transferred = 0
                with local_path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(options.chunk_size), b""):
                        check_transfer_cancelled(options, operation="upload", resource=path)
                        transferred += len(chunk)
                        check_transfer_size(transferred, options, operation="upload", resource=path)
                        digest.update(chunk)
                        yield chunk
                        await asyncio.sleep(0)

            upload_response = await await_transfer(
                self._blob(path).upload_blob(
                    chunks(),
                    overwrite=False,
                    metadata={**options.object_metadata, "agora_operation_id": operation_id},
                ),
                options,
                operation="upload",
                resource=path,
            )
        except Exception as exc:
            if self._conflict(exc):
                raise _StorageConflict from exc
            raise
        transferred_checksum = digest.hexdigest()
        required_checksums = tuple(value for value in (options.expected_sha256, checksum_sha256) if value is not None)
        if any(transferred_checksum != required for required in required_checksums):
            created_etag = (
                upload_response.get("etag")
                if isinstance(upload_response, Mapping)
                else getattr(upload_response, "etag", None)
            )
            if created_etag is None:
                raise PreconditionFailedError(
                    "Upload checksum failed and the created object version is unavailable for safe cleanup.",
                    operation="upload",
                )
            await self._blob(path).delete_blob(
                etag=created_etag,
                match_condition=MatchConditions.IfNotModified,
            )
            raise PreconditionFailedError("Upload checksum did not match the required SHA-256.", operation="upload")
        return await self._created_version(
            self._blob(path),
            path,
            operation_id,
            transferred_checksum,
            self._response_etag(upload_response),
            options,
            started,
            "upload",
        )

    async def create_from_storage(
        self,
        source_path: str,
        target_path: str,
        operation_id: str,
        checksum_sha256: str,
        options: TransferOptions,
    ) -> _ObjectVersion:
        from azure.core import MatchConditions

        source = self._blob(source_path)
        target = self._blob(target_path)
        started = time.monotonic()
        try:
            unbounded_timeout = replace(options, timeout_seconds=None)

            async def perform_transfer():
                download = await await_transfer(
                    source.download_blob(),
                    unbounded_timeout,
                    operation="register",
                    resource=source_path,
                )
                digest = hashlib.sha256()

                async def chunks():
                    transferred = 0
                    async for chunk in download.chunks():
                        check_transfer_cancelled(options, operation="register", resource=source_path)
                        transferred += len(chunk)
                        check_transfer_size(transferred, options, operation="register", resource=source_path)
                        digest.update(chunk)
                        yield chunk

                response = await await_transfer(
                    target.upload_blob(
                        chunks(),
                        overwrite=False,
                        metadata={**options.object_metadata, "agora_operation_id": operation_id},
                    ),
                    unbounded_timeout,
                    operation="register",
                    resource=target_path,
                )
                return response, digest.hexdigest()

            if options.timeout_seconds is None:
                upload_response, transferred_checksum = await perform_transfer()
            else:
                async with asyncio.timeout(options.timeout_seconds):
                    upload_response, transferred_checksum = await perform_transfer()
            if transferred_checksum != checksum_sha256:
                created_etag = (
                    upload_response.get("etag")
                    if isinstance(upload_response, Mapping)
                    else getattr(upload_response, "etag", None)
                )
                if created_etag is not None:
                    await target.delete_blob(etag=created_etag, match_condition=MatchConditions.IfNotModified)
                raise PreconditionFailedError(
                    "External source checksum did not match the requested immutable snapshot.",
                    operation="register",
                )
        except TimeoutError as exc:
            raise TransferTimeoutError(
                f"Transfer exceeded the configured {options.timeout_seconds:g}-second timeout.",
                operation="register",
            ) from exc
        except Exception as exc:
            if self._missing(exc):
                raise FileNotFoundError(source_path) from exc
            if self._conflict(exc):
                raise _StorageConflict from exc
            raise
        return await self._created_version(
            target,
            target_path,
            operation_id,
            checksum_sha256,
            self._response_etag(upload_response),
            options,
            started,
            "register",
        )

    async def exists(self, path: str) -> _ObjectVersion | None:
        from azure.core import MatchConditions

        try:
            blob = self._blob(path)
            properties = await blob.get_blob_properties()
            etag = str(properties.etag)
            download = await blob.download_blob(
                etag=etag,
                match_condition=MatchConditions.IfNotModified,
            )
        except Exception as exc:
            if self._missing(exc):
                return None
            if self._conflict(exc):
                raise ConflictError(
                    "Stored object changed while its ownership and checksum were being verified.",
                    operation="managed_write",
                ) from exc
            raise
        download_properties = getattr(download, "properties", None)
        if str(getattr(download_properties, "etag", "")) != etag:
            raise ConflictError(
                "Stored object changed while its ownership and checksum were being verified.",
                operation="managed_write",
            )
        digest = hashlib.sha256()
        async for chunk in download.chunks():
            digest.update(chunk)
        return _ObjectVersion(
            etag,
            int(properties.size),
            getattr(properties, "metadata", {}).get("agora_operation_id"),
            digest.hexdigest(),
        )

    async def delete_owned(
        self,
        path: str,
        operation_id: str,
        version_token: str | None,
    ) -> DeleteOutcome:
        from azure.core import MatchConditions

        blob = self._blob(path)
        try:
            properties = await blob.get_blob_properties()
        except Exception as exc:
            if self._missing(exc):
                return DeleteOutcome.ABSENT
            raise
        validate_managed_revision_path(path)
        if getattr(properties, "metadata", {}).get("agora_operation_id") != operation_id:
            return DeleteOutcome.MISMATCH
        if version_token is not None and str(properties.etag) != version_token:
            return DeleteOutcome.MISMATCH
        try:
            await blob.delete_blob(etag=properties.etag, match_condition=MatchConditions.IfNotModified)
        except Exception as exc:
            if self._missing(exc):
                return DeleteOutcome.ABSENT
            if self._conflict(exc):
                return DeleteOutcome.MISMATCH
            raise
        return DeleteOutcome.DELETED

    async def list_json(self, prefix: str) -> tuple[tuple[str, dict[str, object]], ...]:
        full_prefix = self._path(prefix).rstrip("/") + "/"
        values: list[tuple[str, dict[str, object]]] = []
        async for item in self._container_client.list_blobs(name_starts_with=full_prefix):  # type: ignore[attr-defined]
            name = item.name
            if not name.endswith(".json"):
                continue
            relative = name[len(self._prefix) + 1 :] if self._prefix else name
            value, _ = await self.read_json(relative)
            if value is not None:
                values.append((relative, value))
        return tuple(values)

    async def reconcile_staging(
        self,
        active_operation_ids: frozenset[str],
        grace_seconds: float,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        del active_operation_ids, grace_seconds
        return (), ()


class ManagedCatalogWriter:
    """Storage-neutral managed write state machine."""

    def __init__(
        self,
        source_id: str,
        backend: _ManagedStorageBackend,
        *,
        max_conflict_retries: int = 4,
        revision_retention: int = 1,
        operation_lease_seconds: float = 30.0,
        interruption_hook: Callable[[str, str], None] | None = None,
    ) -> None:
        if not source_id:
            raise ValueError("source_id must be non-empty")
        if max_conflict_retries < 0:
            raise ValueError("max_conflict_retries must be non-negative")
        if revision_retention < 0:
            raise ValueError("revision_retention must be non-negative")
        if operation_lease_seconds <= 0:
            raise ValueError("operation_lease_seconds must be positive")
        self.source_id = source_id
        self._backend = backend
        self._manifest_path = RESERVED_MANIFEST_PATH
        self._max_conflict_retries = max_conflict_retries
        self._revision_retention = revision_retention
        self._operation_lease_seconds = operation_lease_seconds
        self._interruption_hook = interruption_hook

    def _interrupt(self, step: str, operation_id: str) -> None:
        if self._interruption_hook is not None:
            self._interruption_hook(step, operation_id)

    @staticmethod
    def _intent_path(operation_id: str) -> str:
        return _operation_path(operation_id)

    @staticmethod
    def _receipt_path(operation_id: str) -> str:
        return _managed_receipt_path(operation_id)

    def artifact_reference(self, path: str, artifact_id: str | None = None) -> ArtifactReference:
        """Return the normalized effective reference used for authorization."""
        normalized = _normalize_catalog_path(path)
        return ArtifactReference(
            artifact_id or logical_artifact_id(self.source_id, normalized),
            self.source_id,
        )

    async def read_manifest(self, minimum_generation: int | None = None) -> CatalogManifest:
        """Read committed state directly from the writer's backing store."""
        raw, _ = await self._backend.read_json(self._manifest_path)
        manifest = (
            CatalogManifest.from_mapping(raw)
            if raw is not None
            else CatalogManifest(MANIFEST_VERSION, 0, ())  # internal empty baseline
        )
        if minimum_generation is not None and manifest.generation < minimum_generation:
            raise PreconditionFailedError(
                "The requested committed generation is not visible.",
                operation="read_manifest",
            )
        return manifest

    async def register(
        self,
        request: RegisterArtifactRequest,
        context: RequestContext = RequestContext(),
    ) -> ManagedWriteResult:
        """Snapshot caller-owned bytes without modifying or owning the source."""
        operation_id = _validate_operation_id(request.operation_id)
        path = _normalize_catalog_path(request.path)
        storage_path = normalize_logical_path(request.storage_path)
        if is_reserved_provider_path(storage_path):
            raise InvalidRequestError("External registrations cannot target reserved managed paths.")
        if request.checksum_sha256 is None:
            raise InvalidRequestError(
                "External registration requires checksum_sha256 for an immutable snapshot.",
                operation="register",
            )
        if (
            request.transfer_options.expected_sha256 is not None
            and request.transfer_options.expected_sha256 != request.checksum_sha256
        ):
            raise InvalidRequestError(
                "TransferOptions checksum must match the registered checksum.",
                operation="register",
            )
        artifact_id = request.artifact_id or logical_artifact_id(self.source_id, path)
        revision_path = _revision_path(artifact_id, operation_id, ".data")
        provenance = self._provenance(CatalogOperation.REGISTER, operation_id, context, request)
        intent = self._intent(
            CatalogOperation.REGISTER,
            operation_id,
            path,
            artifact_id,
            request,
            owned_path=revision_path,
            storage_path=revision_path,
            checksum_sha256=request.checksum_sha256,
            metadata=request.metadata,
            provenance=provenance,
        )
        prior, lease_id = await self._begin_operation(operation_id, intent)
        if prior is not None:
            return await self._resume_remove_cleanup(prior)
        try:
            self._interrupt("after_intent", operation_id)
            await emit_transfer_diagnostic(
                request.transfer_options,
                TransferDiagnostic("register", "started", context, revision_path),
            )
            async with self._heartbeat(operation_id, lease_id):
                try:
                    version = await self._backend.create_from_storage(
                        storage_path,
                        revision_path,
                        operation_id,
                        request.checksum_sha256,
                        request.transfer_options,
                    )
                except FileNotFoundError as exc:
                    raise ArtifactNotFoundError("External bytes were not found.", operation="register") from exc
                except _StorageConflict:
                    version = await self._backend.exists(revision_path)
                    if (
                        version is None
                        or version.operation_id != operation_id
                        or version.checksum_sha256 != request.checksum_sha256
                    ):
                        raise ConflictError(
                            "Immutable snapshot path is occupied by bytes not owned by this operation.",
                            operation="register",
                        ) from None
                except BaseException as exc:
                    await emit_transfer_diagnostic(
                        request.transfer_options,
                        TransferDiagnostic(
                            "register",
                            "failed",
                            context,
                            revision_path,
                            error_type=type(exc).__name__,
                        ),
                    )
                    raise
                if request.size_bytes is not None and request.size_bytes != version.size_bytes:
                    await self._backend.delete_owned(revision_path, operation_id, version.token)
                    error = PreconditionFailedError(
                        "Registered size did not match the immutable snapshot.",
                        operation="register",
                    )
                    await emit_transfer_diagnostic(
                        request.transfer_options,
                        TransferDiagnostic(
                            "register",
                            "failed",
                            context,
                            revision_path,
                            error_type=type(error).__name__,
                        ),
                    )
                    raise error
            await emit_transfer_diagnostic(
                request.transfer_options,
                TransferDiagnostic(
                    "register",
                    "completed",
                    context,
                    revision_path,
                    version.size_bytes,
                    version.checksum_sha256,
                ),
            )
            await self._update_operation(
                operation_id,
                lease_id,
                ownership_verified=True,
                owned_version_token=version.token,
            )
            self._interrupt("after_object", operation_id)
            await self._update_operation(operation_id, lease_id, state="commit_ready")
            revision = ManifestRevision(
                revision_id=operation_id,
                storage_path=revision_path,
                content_revision=request.content_revision or request.checksum_sha256,
                created_at=provenance.created_at,
                operation_id=operation_id,
                ownership=ManifestOwnership.MANAGED,
                checksum_sha256=request.checksum_sha256,
                size_bytes=version.size_bytes,
                version_token=version.token,
                provenance=provenance,
            )
            return await self._commit_revision(
                CatalogOperation.REGISTER,
                operation_id,
                path,
                artifact_id,
                request.metadata,
                revision,
                provenance,
                request.expected_generation,
                request.expected_revision_id,
                lease_id,
            )
        except BaseException:
            await self._abandon_operation(operation_id, lease_id)
            raise

    async def upload(
        self,
        request: UploadArtifactRequest,
        context: RequestContext = RequestContext(),
    ) -> ManagedWriteResult:
        """Create an immutable managed revision and commit it to the manifest."""
        return await self._upload(request, context, CatalogOperation.UPLOAD)

    async def promote(
        self,
        request: PromoteOutputRequest,
        context: RequestContext = RequestContext(),
    ) -> ManagedWriteResult:
        """Explicitly copy a scratch output into durable managed storage."""
        if not request.session_id or not request.output_name:
            raise InvalidRequestError("Promotion requires session_id and output_name.", operation="promote")
        return await self._upload(request, context, CatalogOperation.PROMOTE)

    async def _upload(
        self,
        request: UploadArtifactRequest,
        context: RequestContext,
        kind: CatalogOperation,
    ) -> ManagedWriteResult:
        operation_id = _validate_operation_id(request.operation_id)
        path = _normalize_catalog_path(request.path)
        artifact_id = request.artifact_id or logical_artifact_id(self.source_id, path)
        provenance = self._provenance(kind, operation_id, context, request)
        intent = self._intent(
            kind,
            operation_id,
            path,
            artifact_id,
            request,
            metadata=request.metadata,
            provenance=provenance,
        )
        prior, lease_id = await self._begin_operation(operation_id, intent)
        if prior is not None:
            return await self._resume_remove_cleanup(prior)
        local_path = Path(request.local_path)
        if not local_path.is_file():
            await self._abandon_operation(operation_id, lease_id)
            raise ArtifactNotFoundError("Upload source file was not found.", operation=kind.value)
        storage_path = _revision_path(artifact_id, operation_id, ".data")
        await self._update_operation(
            operation_id,
            lease_id,
            owned_path=storage_path,
            storage_path=storage_path,
        )
        try:
            self._interrupt("after_intent", operation_id)
            await emit_transfer_diagnostic(
                request.transfer_options,
                TransferDiagnostic(kind.value, "started", context, storage_path),
            )
            async with self._heartbeat(operation_id, lease_id):
                try:
                    version = await self._backend.create_from_file(
                        storage_path,
                        local_path,
                        operation_id,
                        None,
                        request.transfer_options,
                    )
                except _StorageConflict:
                    version = await self._backend.exists(storage_path)
                    if (
                        version is None
                        or version.operation_id != operation_id
                        or version.checksum_sha256 is None
                        or (
                            request.transfer_options.expected_sha256 is not None
                            and version.checksum_sha256 != request.transfer_options.expected_sha256
                        )
                    ):
                        raise ConflictError(
                            "Immutable revision path is not safely owned by this operation.",
                            operation=kind.value,
                        ) from None
                    if version.operation_id != operation_id:
                        raise ConflictError(
                            "Immutable revision path is occupied by bytes not owned by this operation.",
                            operation=kind.value,
                        ) from None
                except BaseException as exc:
                    await emit_transfer_diagnostic(
                        request.transfer_options,
                        TransferDiagnostic(
                            kind.value,
                            "failed",
                            context,
                            storage_path,
                            error_type=type(exc).__name__,
                        ),
                    )
                    raise
            await emit_transfer_diagnostic(
                request.transfer_options,
                TransferDiagnostic(
                    kind.value,
                    "completed",
                    context,
                    storage_path,
                    version.size_bytes,
                    version.checksum_sha256,
                ),
            )
            checksum = version.checksum_sha256
            if checksum is None:
                raise ConflictError("Transferred revision has no verified checksum.", operation=kind.value)
            await self._update_operation(
                operation_id,
                lease_id,
                checksum_sha256=checksum,
                ownership_verified=True,
                owned_version_token=version.token,
            )
            self._interrupt("after_object", operation_id)
            await self._update_operation(operation_id, lease_id, state="commit_ready")
            revision = ManifestRevision(
                revision_id=operation_id,
                storage_path=storage_path,
                content_revision=checksum,
                created_at=provenance.created_at,
                operation_id=operation_id,
                ownership=ManifestOwnership.MANAGED,
                checksum_sha256=checksum,
                size_bytes=version.size_bytes,
                version_token=version.token,
                provenance=provenance,
            )
            return await self._commit_revision(
                kind,
                operation_id,
                path,
                artifact_id,
                request.metadata,
                revision,
                provenance,
                request.expected_generation,
                request.expected_revision_id,
                lease_id,
            )
        except BaseException:
            await self._abandon_operation(operation_id, lease_id)
            raise

    async def remove(
        self,
        request: RemoveArtifactRequest,
        context: RequestContext = RequestContext(),
    ) -> ManagedWriteResult:
        """Commit a tombstone before collecting any managed bytes."""
        operation_id = _validate_operation_id(request.operation_id)
        if request.reference.source_id != self.source_id:
            raise ArtifactNotFoundError("Artifact was not found.", operation="remove")
        if request.reference.revision is not None:
            raise InvalidRequestError(
                "Managed removal uses expected_revision_id rather than a catalog revision number.",
                operation="remove",
            )
        intent = self._intent(
            CatalogOperation.REMOVE,
            operation_id,
            "",
            request.reference.artifact_id,
            request,
            provenance=self._provenance(CatalogOperation.REMOVE, operation_id, context, request),
        )
        prior, lease_id = await self._begin_operation(operation_id, intent)
        if prior is not None:
            return await self._resume_remove_cleanup(prior)
        provenance = self._provenance(CatalogOperation.REMOVE, operation_id, context, request)
        try:
            self._interrupt("after_intent", operation_id)
            await self._update_operation(operation_id, lease_id, state="commit_ready")
            return await self._remove_under_lease(request, provenance, operation_id, lease_id)
        except BaseException:
            await self._abandon_operation(operation_id, lease_id)
            raise

    async def _remove_under_lease(
        self,
        request: RemoveArtifactRequest,
        provenance: ManifestProvenance,
        operation_id: str,
        lease_id: str,
    ) -> ManagedWriteResult:
        result: ManagedWriteResult | None = None
        removed: ManifestArtifact | None = None
        async with self._heartbeat(operation_id, lease_id):
            async with self._backend.serialized():
                await self._assert_active_operation(operation_id, lease_id)
                for attempt in range(self._max_conflict_retries + 1):
                    await self._assert_active_operation(operation_id, lease_id)
                    try:
                        manifest, token = await self._load()
                        await self._assert_active_operation(operation_id, lease_id)
                        if _commit_fence_generation(manifest, operation_id) is not None:
                            cleared = replace(
                                manifest,
                                generation=manifest.generation + 1,
                                commit_fences=_without_commit_fence(manifest, operation_id),
                            )
                            try:
                                await self._commit_manifest(cleared, token)
                            except _StorageConflict:
                                if attempt == self._max_conflict_retries:
                                    await self._make_abandonable(operation_id, lease_id)
                                    raise RetryExhaustedError(
                                        "Manifest fence retries were exhausted.",
                                        operation="remove",
                                    ) from None
                                continue
                            continue
                        current = self._find_artifact(manifest, request.reference.artifact_id)
                        if current is None or current.deleted_at is not None:
                            raise ArtifactNotFoundError("Artifact was not found.", operation="remove")
                        operation_state = await self._assert_active_operation(operation_id, lease_id)
                        self._check_preconditions(
                            manifest,
                            current,
                            request.expected_generation,
                            request.expected_revision_id,
                            "remove",
                            operation_state.get("recovery_generation"),
                            operation_state.get("recovery_expected_generation"),
                        )
                        removal = ManifestRemoval(
                            operation_id=operation_id,
                            generation=manifest.generation + 1,
                            revision_id=current.revision_id,
                            storage_path=current.storage_path,
                            garbage_collect=request.garbage_collect,
                            revisions=current.revisions,
                            provenance=provenance,
                        )
                        tombstone = replace(
                            current,
                            deleted_at=_utc_now(),
                            provenance=provenance,
                            removals=(*current.removals, removal),
                        )
                        updated = self._replace_artifact(manifest, tombstone)
                        updated = replace(
                            updated,
                            commit_fences=_without_commit_fence(updated, operation_id),
                        )
                    except BaseException:
                        await self._make_abandonable(operation_id, lease_id)
                        raise
                    try:
                        await self._assert_active_operation(operation_id, lease_id)
                        await self._commit_manifest(updated, token)
                    except _StorageConflict:
                        if attempt == self._max_conflict_retries:
                            await self._make_abandonable(operation_id, lease_id)
                            raise RetryExhaustedError(
                                "Manifest conflict retries were exhausted.",
                                operation="remove",
                            ) from None
                        continue
                    removed = tombstone
                    result = ManagedWriteResult(
                        operation_id,
                        self.source_id,
                        current.artifact_id or request.reference.artifact_id,
                        updated.generation,
                        current.revision_id,
                        current.storage_path,
                        deleted=True,
                        cleanup_pending=request.garbage_collect,
                    )
                    break
            assert result is not None and removed is not None
            self._interrupt("after_manifest", operation_id)
            await self._write_receipt(result)
            self._interrupt("after_receipt", operation_id)
            if request.garbage_collect and await self._collect_revisions(removed):
                result = replace(result, cleanup_pending=False)
                await self._write_receipt(result, replace_existing=True)
            return result

    async def _commit_revision(
        self,
        kind: CatalogOperation,
        operation_id: str,
        path: str,
        artifact_id: str,
        metadata: ArtifactMetadata,
        revision: ManifestRevision,
        provenance: ManifestProvenance,
        expected_generation: int | None,
        expected_revision_id: str | None,
        lease_id: str,
    ) -> ManagedWriteResult:
        async with self._heartbeat(operation_id, lease_id):
            return await self._commit_revision_body(
                kind,
                operation_id,
                path,
                artifact_id,
                metadata,
                revision,
                provenance,
                expected_generation,
                expected_revision_id,
                lease_id,
            )

    async def _commit_revision_body(
        self,
        kind: CatalogOperation,
        operation_id: str,
        path: str,
        artifact_id: str,
        metadata: ArtifactMetadata,
        revision: ManifestRevision,
        provenance: ManifestProvenance,
        expected_generation: int | None,
        expected_revision_id: str | None,
        lease_id: str,
    ) -> ManagedWriteResult:
        result: ManagedWriteResult | None = None
        async with self._backend.serialized():
            await self._assert_active_operation(operation_id, lease_id)
            for attempt in range(self._max_conflict_retries + 1):
                await self._assert_active_operation(operation_id, lease_id)
                try:
                    manifest, token = await self._load()
                    await self._assert_active_operation(operation_id, lease_id)
                    if _commit_fence_generation(manifest, operation_id) is not None:
                        cleared = replace(
                            manifest,
                            generation=manifest.generation + 1,
                            commit_fences=_without_commit_fence(manifest, operation_id),
                        )
                        try:
                            await self._commit_manifest(cleared, token)
                        except _StorageConflict:
                            if attempt == self._max_conflict_retries:
                                await self._make_abandonable(operation_id, lease_id)
                                raise RetryExhaustedError(
                                    "Manifest fence retries were exhausted.",
                                    operation=kind.value,
                                ) from None
                            continue
                        continue
                    current = self._find_artifact(manifest, artifact_id)
                    at_path = next((item for item in manifest.artifacts if item.path == path), None)
                    if at_path is not None and at_path.artifact_id != artifact_id:
                        raise ConflictError("Logical path is retained by another artifact.", operation=kind.value)
                    if current is not None and current.path != path:
                        raise ConflictError("Artifact ID is retained at another logical path.", operation=kind.value)
                    operation_state = await self._assert_active_operation(operation_id, lease_id)
                    self._check_preconditions(
                        manifest,
                        current,
                        expected_generation,
                        expected_revision_id,
                        kind.value,
                        operation_state.get("recovery_generation"),
                        operation_state.get("recovery_expected_generation"),
                    )
                    revisions = current.revisions if current is not None else ()
                    committed_revision = replace(
                        revision,
                        committed_generation=manifest.generation + 1,
                    )
                    existing_revision = next(
                        (item for item in revisions if item.revision_id == revision.revision_id),
                        None,
                    )
                    if existing_revision is not None and existing_revision != committed_revision:
                        raise ConflictError("Operation revision identity was reused.", operation=kind.value)
                    if existing_revision is None:
                        revisions = (*revisions, committed_revision)
                    name = metadata.name or (current.name if current else None)
                    description = (
                        metadata.description
                        if metadata.description is not None
                        else (current.description if current else None)
                    )
                    domain = metadata.domain if metadata.domain is not None else (current.domain if current else None)
                    media_type = (
                        metadata.media_type
                        if metadata.media_type is not None
                        else (current.media_type if current else None)
                    )
                    aliases = metadata.aliases or (current.aliases if current else ())
                    artifact = ManifestArtifact(
                        path=path,
                        artifact_id=artifact_id,
                        name=name,
                        description=description,
                        domain=domain,
                        media_type=media_type,
                        size_bytes=revision.size_bytes,
                        content_revision=revision.content_revision,
                        metadata_revision=_fingerprint(
                            {
                                "name": name,
                                "description": description,
                                "domain": domain,
                                "media_type": media_type,
                                "aliases": list(aliases),
                            }
                        ),
                        checksum_sha256=revision.checksum_sha256,
                        aliases=aliases,
                        storage_path=revision.storage_path,
                        revision_id=revision.revision_id,
                        revisions=revisions,
                        removals=current.removals if current is not None else (),
                        ownership=revision.ownership,
                        provenance=provenance,
                    )
                    updated = self._replace_artifact(manifest, artifact)
                    updated = replace(
                        updated,
                        commit_fences=_without_commit_fence(updated, operation_id),
                    )
                except BaseException:
                    await self._make_abandonable(operation_id, lease_id)
                    raise
                try:
                    await self._assert_active_operation(operation_id, lease_id)
                    await self._commit_manifest(updated, token)
                except _StorageConflict:
                    if attempt == self._max_conflict_retries:
                        await self._make_abandonable(operation_id, lease_id)
                        raise RetryExhaustedError(
                            "Manifest conflict retries were exhausted.",
                            operation=kind.value,
                        ) from None
                    continue
                result = ManagedWriteResult(
                    operation_id,
                    self.source_id,
                    artifact_id,
                    updated.generation,
                    revision.revision_id,
                    revision.storage_path,
                )
                break
        assert result is not None
        self._interrupt("after_manifest", operation_id)
        await self._write_receipt(result)
        self._interrupt("after_receipt", operation_id)
        return result

    async def _load(self) -> tuple[CatalogManifest, str | None]:
        raw, token = await self._backend.read_json(self._manifest_path)
        if raw is None:
            return CatalogManifest(MANIFEST_VERSION, 0, ()), token
        return CatalogManifest.from_mapping(raw), token

    async def _commit_manifest(self, manifest: CatalogManifest, token: str | None) -> None:
        value = manifest.to_mapping()
        CatalogManifest.from_mapping(value)
        if len(_canonical_json(value)) > MAX_MANIFEST_BYTES:
            raise InvalidRequestError("Managed manifest exceeds the size limit.", operation="managed_write")
        await self._backend.replace_json(self._manifest_path, value, token)

    @staticmethod
    def _replace_artifact(manifest: CatalogManifest, artifact: ManifestArtifact) -> CatalogManifest:
        artifacts = [item for item in manifest.artifacts if item.artifact_id != artifact.artifact_id]
        artifacts.append(artifact)
        artifacts.sort(key=lambda item: (item.path, item.artifact_id or ""))
        return CatalogManifest(
            MANIFEST_VERSION,
            manifest.generation + 1,
            tuple(artifacts),
            manifest.commit_fences,
        )

    @staticmethod
    def _find_artifact(manifest: CatalogManifest, artifact_id: str) -> ManifestArtifact | None:
        return next((item for item in manifest.artifacts if item.artifact_id == artifact_id), None)

    @staticmethod
    def _check_preconditions(
        manifest: CatalogManifest,
        current: ManifestArtifact | None,
        expected_generation: int | None,
        expected_revision_id: str | None,
        operation: str,
        recovery_generation: object = None,
        recovery_expected_generation: object = None,
    ) -> None:
        generation_matches_recovery = (
            isinstance(recovery_generation, int)
            and not isinstance(recovery_generation, bool)
            and manifest.generation == recovery_generation
            and isinstance(recovery_expected_generation, int)
            and not isinstance(recovery_expected_generation, bool)
            and expected_generation == recovery_expected_generation
        )
        if (
            expected_generation is not None
            and manifest.generation != expected_generation
            and not generation_matches_recovery
        ):
            raise PreconditionFailedError("Manifest generation precondition failed.", operation=operation)
        if expected_revision_id is not None:
            actual = current.revision_id if current is not None else None
            if actual != expected_revision_id:
                raise PreconditionFailedError("Artifact revision precondition failed.", operation=operation)

    @staticmethod
    def _provenance(
        kind: CatalogOperation,
        operation_id: str,
        context: RequestContext,
        request: object | None,
    ) -> ManifestProvenance:
        return ManifestProvenance(
            operation_id=operation_id,
            kind=kind.value,
            created_at=_utc_now(),
            caller_id=context.caller_id,
            source_uri=None,
            session_id=getattr(request, "session_id", None),
            output_name=getattr(request, "output_name", None),
        )

    def _intent(
        self,
        kind: CatalogOperation,
        operation_id: str,
        path: str,
        artifact_id: str,
        request: object,
        *,
        owned_path: str | None = None,
        storage_path: str | None = None,
        checksum_sha256: str | None = None,
        metadata: ArtifactMetadata | None = None,
        provenance: ManifestProvenance,
    ) -> dict[str, object]:
        identity = {
            "kind": kind.value,
            "source_id": self.source_id,
            "path": path,
            "artifact_id": artifact_id,
            "owned_path": owned_path,
            "storage_path": storage_path,
            "checksum_sha256": checksum_sha256,
            "ownership_verified": False,
            "owned_version_token": None,
            "expected_generation": getattr(request, "expected_generation", None),
            "expected_revision_id": getattr(request, "expected_revision_id", None),
            "metadata": (
                {
                    "name": metadata.name,
                    "description": metadata.description,
                    "domain": metadata.domain,
                    "media_type": metadata.media_type,
                    "aliases": list(metadata.aliases),
                }
                if metadata is not None
                else None
            ),
            "session_id": getattr(request, "session_id", None),
            "output_name": getattr(request, "output_name", None),
            "input_path_digest": (
                hashlib.sha256(str(getattr(request, "local_path")).encode()).hexdigest()
                if hasattr(request, "local_path")
                else None
            ),
            "source_storage_path": getattr(request, "storage_path", None),
            "content_revision": getattr(request, "content_revision", None),
            "size_bytes": getattr(request, "size_bytes", None),
            "version_token": getattr(request, "version_token", None),
            "garbage_collect": getattr(request, "garbage_collect", None),
            "transfer": (
                {
                    "max_bytes": request.transfer_options.max_bytes,
                    "quota_bytes": request.transfer_options.quota_bytes,
                    "timeout_seconds": request.transfer_options.timeout_seconds,
                    "chunk_size": request.transfer_options.chunk_size,
                    "expected_sha256": request.transfer_options.expected_sha256,
                    "object_metadata": dict(request.transfer_options.object_metadata),
                }
                if hasattr(request, "transfer_options")
                else None
            ),
        }
        return {
            **identity,
            "operation_id": operation_id,
            "created_at": provenance.created_at,
            "fingerprint": _fingerprint(identity),
        }

    async def _begin_operation(
        self,
        operation_id: str,
        intent: Mapping[str, object],
    ) -> tuple[ManagedWriteResult | None, str]:
        lease_id = uuid.uuid4().hex
        receipt, _ = await self._backend.read_json(self._receipt_path(operation_id))
        if receipt is not None:
            stored_intent, _ = await self._backend.read_json(self._intent_path(operation_id))
            if stored_intent is None or stored_intent.get("fingerprint") != intent.get("fingerprint"):
                raise ConflictError("Operation ID was reused for a different request.", operation="managed_write")
            return self._result_from_mapping(receipt), lease_id
        manifest, _ = await self._load()
        committed = next(
            (
                artifact
                for artifact in manifest.artifacts
                if (artifact.provenance is not None and artifact.provenance.operation_id == operation_id)
                or any(revision.operation_id == operation_id for revision in artifact.revisions)
                or any(removal.operation_id == operation_id for removal in artifact.removals)
            ),
            None,
        )
        if committed is not None:
            stored_intent, _ = await self._backend.read_json(self._intent_path(operation_id))
            if stored_intent is None or stored_intent.get("fingerprint") != intent.get("fingerprint"):
                raise ConflictError("Operation ID was reused for a different request.", operation="managed_write")
            removal = next(
                (item for item in committed.removals if item.operation_id == operation_id),
                None,
            )
            committed_revision = next(
                (item for item in committed.revisions if item.operation_id == operation_id),
                None,
            )
            cleanup_pending = removal is not None and removal.garbage_collect
            result = ManagedWriteResult(
                operation_id,
                self.source_id,
                committed.artifact_id or "",
                (
                    removal.generation
                    if removal is not None
                    else (
                        committed_revision.committed_generation
                        if committed_revision is not None and committed_revision.committed_generation is not None
                        else manifest.generation
                    )
                ),
                removal.revision_id
                if removal is not None
                else (committed_revision.revision_id if committed_revision is not None else committed.revision_id),
                removal.storage_path
                if removal is not None
                else (committed_revision.storage_path if committed_revision is not None else committed.storage_path),
                deleted=removal is not None or committed.deleted_at is not None,
                cleanup_pending=cleanup_pending,
            )
            await self._write_receipt(result)
            return result, lease_id
        active_intent = {
            **intent,
            "state": "active",
            "lease_id": lease_id,
            "lease_until": self._lease_until(),
        }
        created = await self._backend.create_json(self._intent_path(operation_id), active_intent)
        if not created:
            async with self._backend.serialized():
                stored, token = await self._backend.read_json(self._intent_path(operation_id))
                if stored is None or stored.get("fingerprint") != intent.get("fingerprint"):
                    raise ConflictError("Operation ID was reused for a different request.", operation="managed_write")
                state = stored.get("state")
                if state in {"commit_ready", "fencing", "reconciling"}:
                    raise ConflictError(
                        "The commit_ready operation's manifest outcome is being recovered before retry.",
                        operation="managed_write",
                    )
                if state == "active" and not self._lease_expired(stored):
                    raise ConflictError("The operation is already active.", operation="managed_write")
                claimed = {
                    **stored,
                    "state": "active",
                    "lease_id": lease_id,
                    "lease_until": self._lease_until(),
                }
                try:
                    await self._backend.replace_json(self._intent_path(operation_id), claimed, token)
                except _StorageConflict as exc:
                    raise ConflictError(
                        "The operation lease was claimed concurrently.", operation="managed_write"
                    ) from exc
        return None, lease_id

    def _lease_until(self) -> str:
        return datetime.fromtimestamp(
            time.time() + self._operation_lease_seconds,
            timezone.utc,
        ).isoformat()

    @staticmethod
    def _lease_expired(intent: Mapping[str, object]) -> bool:
        value = intent.get("lease_until")
        if not isinstance(value, str):
            return True
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")) <= datetime.now(timezone.utc)
        except ValueError:
            return True

    async def _update_operation(self, operation_id: str, lease_id: str, **updates: object) -> None:
        path = self._intent_path(operation_id)
        async with self._backend.serialized():
            intent, token = await self._backend.read_json(path)
            if intent is None:
                raise ConflictError("Operation state is unavailable.", operation="managed_write")
            if intent.get("state") not in {"active", "commit_ready"} or intent.get("lease_id") != lease_id:
                raise ConflictError("Operation lease is no longer active.", operation="managed_write")
            updated = {**intent, **updates, "lease_until": self._lease_until()}
            try:
                await self._backend.replace_json(path, updated, token)
            except _StorageConflict as exc:
                raise ConflictError("Operation state changed concurrently.", operation="managed_write") from exc

    async def _assert_active_operation(self, operation_id: str, lease_id: str) -> dict[str, object]:
        intent, _ = await self._backend.read_json(self._intent_path(operation_id))
        if (
            intent is None
            or intent.get("state") not in {"active", "commit_ready"}
            or intent.get("lease_id") != lease_id
            or self._lease_expired(intent)
        ):
            raise ConflictError("Operation was abandoned before commit.", operation="managed_write")
        return intent

    async def _abandon_operation(self, operation_id: str, lease_id: str) -> None:
        path = self._intent_path(operation_id)
        try:
            async with self._backend.serialized():
                intent, token = await self._backend.read_json(path)
                if intent is None or intent.get("lease_id") != lease_id or intent.get("state") != "active":
                    return
                await self._backend.replace_json(path, {**intent, "state": "abandoned"}, token)
        except Exception:
            return

    async def _make_abandonable(self, operation_id: str, lease_id: str) -> None:
        path = self._intent_path(operation_id)
        intent, token = await self._backend.read_json(path)
        if intent is None or intent.get("lease_id") != lease_id or intent.get("state") != "commit_ready":
            return
        try:
            await self._backend.replace_json(path, {**intent, "state": "active"}, token)
        except _StorageConflict:
            return

    @asynccontextmanager
    async def _heartbeat(self, operation_id: str, lease_id: str) -> AsyncIterator[None]:
        stopped = asyncio.Event()

        async def renew() -> None:
            while not stopped.is_set():
                try:
                    await asyncio.wait_for(stopped.wait(), timeout=self._operation_lease_seconds / 3)
                except TimeoutError:
                    await self._update_operation(operation_id, lease_id)

        task = asyncio.create_task(renew())
        try:
            yield
        finally:
            stopped.set()
            await task

    async def _resume_remove_cleanup(self, result: ManagedWriteResult) -> ManagedWriteResult:
        if not result.deleted or not result.cleanup_pending:
            return result
        manifest, _ = await self._load()
        artifact = self._find_artifact(manifest, result.artifact_id)
        if artifact is None:
            return result
        removal = next(
            (item for item in artifact.removals if item.operation_id == result.operation_id),
            None,
        )
        if removal is None:
            return result
        if await self._collect_revision_set(removal.revisions):
            result = replace(result, cleanup_pending=False)
            await self._write_receipt(result, replace_existing=True)
        return result

    async def _write_receipt(self, result: ManagedWriteResult, *, replace_existing: bool = False) -> None:
        value = asdict(result)
        path = self._receipt_path(result.operation_id)
        if replace_existing:
            _, token = await self._backend.read_json(path)
            await self._backend.replace_json(path, value, token)
        elif not await self._backend.create_json(path, value):
            existing, _ = await self._backend.read_json(path)
            if existing != value:
                raise ConflictError("Operation receipt conflicts with committed result.", operation="managed_write")

    @staticmethod
    def _result_from_mapping(value: Mapping[str, object]) -> ManagedWriteResult:
        generation = value["generation"]
        if not isinstance(generation, int) or isinstance(generation, bool):
            raise InvalidRequestError("Operation receipt generation is invalid.", operation="managed_write")
        return ManagedWriteResult(
            operation_id=str(value["operation_id"]),
            source_id=str(value["source_id"]),
            artifact_id=str(value["artifact_id"]),
            generation=generation,
            revision_id=str(value["revision_id"]) if value.get("revision_id") is not None else None,
            storage_path=str(value["storage_path"]) if value.get("storage_path") is not None else None,
            deleted=bool(value.get("deleted", False)),
            cleanup_pending=bool(value.get("cleanup_pending", False)),
        )

    async def _collect_revisions(self, artifact: ManifestArtifact) -> bool:
        return await self._collect_revision_set(artifact.revisions)

    async def _collect_revision_set(self, revisions: tuple[ManifestRevision, ...]) -> bool:
        retained = sorted(revisions, key=lambda item: item.created_at, reverse=True)
        success = True
        for revision in retained[self._revision_retention :]:
            if revision.ownership is ManifestOwnership.EXTERNAL:
                continue
            outcome = await self._backend.delete_owned(
                revision.storage_path,
                revision.operation_id,
                revision.version_token,
            )
            success = outcome in {DeleteOutcome.DELETED, DeleteOutcome.ABSENT} and success
        return success

    @staticmethod
    def _find_operation_artifact(manifest: CatalogManifest, operation_id: str) -> ManifestArtifact | None:
        return next(
            (
                item
                for item in manifest.artifacts
                if (item.provenance and item.provenance.operation_id == operation_id)
                or any(revision.operation_id == operation_id for revision in item.revisions)
                or any(removal.operation_id == operation_id for removal in item.removals)
            ),
            None,
        )

    async def _fence_expired_commit(
        self,
        intent_path: str,
        intent: dict[str, object],
        intent_token: str | None,
        operation_id: str,
    ) -> tuple[ManifestArtifact | None, CatalogManifest | None, dict[str, object] | None]:
        if intent.get("state") == "commit_ready":
            claimed = {
                **intent,
                "state": "fencing",
                "reconciliation_claim": uuid.uuid4().hex,
            }
            try:
                intent_token = await self._backend.replace_json(intent_path, claimed, intent_token)
            except _StorageConflict:
                return None, None, None
            intent = claimed
        elif intent.get("state") != "fencing":
            return None, None, None

        for attempt in range(self._max_conflict_retries + 1):
            manifest, manifest_token = await self._load()
            artifact = self._find_operation_artifact(manifest, operation_id)
            if artifact is not None:
                return artifact, manifest, intent
            fence_generation = _commit_fence_generation(manifest, operation_id)
            if fence_generation is not None:
                break
            if isinstance(intent.get("fence_generation"), int):
                return None, manifest, intent
            fence_generation = manifest.generation + 1
            fenced = CatalogManifest(
                MANIFEST_VERSION,
                fence_generation,
                manifest.artifacts,
                (*manifest.commit_fences, f"{operation_id}:{fence_generation}"),
            )
            try:
                await self._commit_manifest(fenced, manifest_token)
            except _StorageConflict:
                if attempt == self._max_conflict_retries:
                    return None, None, None
                continue
            manifest, _ = await self._load()
            artifact = self._find_operation_artifact(manifest, operation_id)
            if artifact is not None:
                return artifact, manifest, intent
            if _commit_fence_generation(manifest, operation_id) is None:
                return None, None, None
            break
        else:
            return None, None, None

        current, current_token = await self._backend.read_json(intent_path)
        if current is None or current.get("state") != "fencing":
            return None, None, None
        fence_generation = _commit_fence_generation(manifest, operation_id)
        if fence_generation is not None and (
            current.get("fence_generation") != fence_generation
            or current.get("pre_fence_generation") != fence_generation - 1
        ):
            updated = {
                **current,
                "fence_generation": fence_generation,
                "pre_fence_generation": fence_generation - 1,
            }
            try:
                await self._backend.replace_json(intent_path, updated, current_token)
            except _StorageConflict:
                return None, None, None
            current = updated
        return None, manifest, current

    async def _finish_fenced_operation(
        self,
        intent_path: str,
        intent: Mapping[str, object],
        operation_id: str,
    ) -> bool:
        current, current_token = await self._backend.read_json(intent_path)
        if (
            current is None
            or current.get("state") != "fencing"
            or current.get("reconciliation_claim") != intent.get("reconciliation_claim")
        ):
            return False
        fence_generation = intent.get("fence_generation")
        cleared_generation, uncontended = await self._clear_commit_fence(
            operation_id,
            fence_generation if isinstance(fence_generation, int) else None,
        )
        if cleared_generation is None:
            return False
        current, current_token = await self._backend.read_json(intent_path)
        if (
            current is None
            or current.get("state") != "fencing"
            or current.get("reconciliation_claim") != intent.get("reconciliation_claim")
        ):
            return False
        abandoned = {
            **current,
            "state": "abandoned",
            "fenced_at": _utc_now(),
            **(
                {
                    "recovery_generation": cleared_generation,
                    "recovery_expected_generation": current.get("pre_fence_generation"),
                }
                if uncontended and isinstance(current.get("pre_fence_generation"), int)
                else {}
            ),
        }
        try:
            await self._backend.replace_json(intent_path, abandoned, current_token)
        except _StorageConflict:
            return False
        return True

    async def _clear_commit_fence(
        self,
        operation_id: str,
        expected_fence_generation: int | None = None,
    ) -> tuple[int | None, bool]:
        for _ in range(self._max_conflict_retries + 1):
            try:
                manifest, token = await self._load()
                fence_generation = _commit_fence_generation(manifest, operation_id)
                if fence_generation is None:
                    return (
                        manifest.generation,
                        expected_fence_generation is not None and manifest.generation == expected_fence_generation + 1,
                    )
                cleared = CatalogManifest(
                    MANIFEST_VERSION,
                    manifest.generation + 1,
                    manifest.artifacts,
                    _without_commit_fence(manifest, operation_id),
                )
                await self._commit_manifest(cleared, token)
                return cleared.generation, manifest.generation == fence_generation
            except _StorageConflict:
                continue
            except Exception:
                return None, False
        return None, False

    async def _finish_cleanup_operation(
        self,
        intent_path: str,
        intent: Mapping[str, object],
    ) -> bool:
        current, token = await self._backend.read_json(intent_path)
        if (
            current is None
            or current.get("state") != "reconciling"
            or current.get("reconciliation_claim") != intent.get("reconciliation_claim")
        ):
            return False
        try:
            await self._backend.replace_json(intent_path, {**current, "state": "abandoned"}, token)
        except _StorageConflict:
            return False
        return True

    async def reconcile(self, *, grace_seconds: float = 300.0) -> ReconciliationReport:
        """Claim abandoned operations before recovery or owned-orphan cleanup."""
        if grace_seconds < 0:
            raise ValueError("grace_seconds must be non-negative")
        recovered: list[str] = []
        removed: list[str] = []
        deferred: list[str] = []
        active_operation_ids: set[str] = set()
        failures: dict[str, str] = {}
        for intent_path, listed_intent in await self._backend.list_json(RESERVED_OPERATIONS_PREFIX.rstrip("/")):
            operation_id = str(listed_intent.get("operation_id", ""))
            try:
                async with self._backend.serialized():
                    intent, token = await self._backend.read_json(intent_path)
                    if intent is None:
                        continue
                    receipt, _ = await self._backend.read_json(self._receipt_path(operation_id))
                    if receipt is not None:
                        result = self._result_from_mapping(receipt)
                        resumed = await self._resume_remove_cleanup(result)
                        if resumed != result:
                            recovered.append(operation_id)
                        continue
                    manifest, _ = await self._load()
                    artifact = self._find_operation_artifact(manifest, operation_id)
                    if artifact is not None:
                        pass
                    else:
                        created_at = datetime.fromisoformat(str(intent["created_at"]).replace("Z", "+00:00"))
                        age = (datetime.now(timezone.utc) - created_at).total_seconds()
                        if age < grace_seconds or (
                            intent.get("state") in {"active", "commit_ready"} and not self._lease_expired(intent)
                        ):
                            active_operation_ids.add(operation_id)
                            deferred.append(operation_id)
                            continue
                        if intent.get("state") in {"commit_ready", "fencing"}:
                            artifact, manifest, intent = await self._fence_expired_commit(
                                intent_path,
                                intent,
                                token,
                                operation_id,
                            )
                            if manifest is None or intent is None:
                                deferred.append(operation_id)
                                continue
                        elif intent.get("state") == "reconciling":
                            pass
                        else:
                            claim_id = uuid.uuid4().hex
                            claimed = {**intent, "state": "reconciling", "reconciliation_claim": claim_id}
                            try:
                                await self._backend.replace_json(intent_path, claimed, token)
                            except _StorageConflict:
                                deferred.append(operation_id)
                                continue
                            intent = claimed
                            manifest, _ = await self._load()
                            artifact = self._find_operation_artifact(manifest, operation_id)
                if artifact is not None:
                    removal = next(
                        (item for item in artifact.removals if item.operation_id == operation_id),
                        None,
                    )
                    committed_revision = next(
                        (item for item in artifact.revisions if item.operation_id == operation_id),
                        None,
                    )
                    result = ManagedWriteResult(
                        operation_id,
                        self.source_id,
                        artifact.artifact_id or "",
                        (
                            removal.generation
                            if removal is not None
                            else (
                                committed_revision.committed_generation
                                if committed_revision is not None
                                and committed_revision.committed_generation is not None
                                else manifest.generation
                            )
                        ),
                        removal.revision_id
                        if removal is not None
                        else (
                            committed_revision.revision_id if committed_revision is not None else artifact.revision_id
                        ),
                        removal.storage_path
                        if removal is not None
                        else (
                            committed_revision.storage_path if committed_revision is not None else artifact.storage_path
                        ),
                        deleted=removal is not None or artifact.deleted_at is not None,
                        cleanup_pending=removal.garbage_collect if removal is not None else False,
                    )
                    await self._write_receipt(result)
                    result = await self._resume_remove_cleanup(result)
                    recovered.append(operation_id)
                    continue
                owned_path = intent.get("owned_path")
                owned_version_token = intent.get("owned_version_token")
                if isinstance(owned_path, str) and (
                    intent.get("ownership_verified") is not True or not isinstance(owned_version_token, str)
                ):
                    owned = await self._backend.exists(owned_path)
                    if owned is not None and owned.operation_id == operation_id:
                        current, token = await self._backend.read_json(intent_path)
                        if (
                            current is not None
                            and current.get("state") in {"fencing", "reconciling"}
                            and current.get("reconciliation_claim") == intent.get("reconciliation_claim")
                        ):
                            recovered_intent = {
                                **current,
                                "ownership_verified": True,
                                "owned_version_token": owned.token,
                                "checksum_sha256": owned.checksum_sha256,
                            }
                            try:
                                await self._backend.replace_json(intent_path, recovered_intent, token)
                            except _StorageConflict:
                                deferred.append(operation_id)
                                continue
                            intent = recovered_intent
                            owned_version_token = owned.token
                if (
                    isinstance(owned_path, str)
                    and intent.get("ownership_verified") is True
                    and isinstance(owned_version_token, str)
                ):
                    outcome = await self._backend.delete_owned(owned_path, operation_id, owned_version_token)
                    if outcome in {DeleteOutcome.DELETED, DeleteOutcome.ABSENT}:
                        if intent.get("state") != "fencing" or await self._finish_fenced_operation(
                            intent_path,
                            intent,
                            operation_id,
                        ):
                            if intent.get("state") != "reconciling" or await self._finish_cleanup_operation(
                                intent_path,
                                intent,
                            ):
                                removed.append(operation_id)
                            else:
                                deferred.append(operation_id)
                        else:
                            deferred.append(operation_id)
                    else:
                        failures[operation_id] = "Owned orphan could not be verified or removed."
                else:
                    if intent.get("state") == "fencing":
                        await self._finish_fenced_operation(intent_path, intent, operation_id)
                    elif intent.get("state") == "reconciling":
                        await self._finish_cleanup_operation(intent_path, intent)
                    deferred.append(operation_id)
            except Exception as exc:
                failures[operation_id] = type(exc).__name__
        if failures:
            raise ReconciliationError(
                "One or more interrupted operations could not be reconciled.",
                operation="reconcile",
            )
        async with self._backend.serialized():
            removed_staging, deferred_staging = await self._backend.reconcile_staging(
                frozenset(active_operation_ids),
                grace_seconds,
            )
        return ReconciliationReport(
            tuple(recovered),
            tuple(removed),
            tuple(deferred),
            removed_staging,
            deferred_staging,
            failures,
        )


class AuthorizedManagedCatalogWriter:
    """Authorize every mutation before metadata or byte side effects."""

    def __init__(self, writer: ManagedCatalogWriter, authorizer: CatalogAuthorizer) -> None:
        self._writer = writer
        self._authorizer = authorizer

    async def capabilities(self, context: RequestContext) -> tuple[SourceCapabilities, ...]:
        """Return write operations allowed for this session and source."""
        allowed = {
            operation
            for operation in WRITE_OPERATIONS
            if await self._authorizer.authorize(
                CatalogAuthorizationRequest(operation, self._writer.source_id),
                context,
            )
        }
        return (SourceCapabilities(self._writer.source_id, frozenset(allowed)),) if allowed else ()

    async def _require(
        self,
        operation: CatalogOperation,
        context: RequestContext,
        reference: ArtifactReference | None = None,
    ) -> None:
        allowed = await self._authorizer.authorize(
            CatalogAuthorizationRequest(operation, self._writer.source_id, reference),
            context,
        )
        if not allowed:
            raise PermissionDeniedError("Catalog mutation is not permitted.", operation=operation.value)

    async def _require_create(
        self,
        operation: CatalogOperation,
        path: str,
        artifact_id: str | None,
        context: RequestContext,
    ) -> None:
        await self._require(operation, context)
        await self._require(operation, context, self._writer.artifact_reference(path, artifact_id))

    async def register(
        self,
        request: RegisterArtifactRequest,
        context: RequestContext = RequestContext(),
    ) -> ManagedWriteResult:
        await self._require_create(CatalogOperation.REGISTER, request.path, request.artifact_id, context)
        return await self._writer.register(request, context)

    async def upload(
        self,
        request: UploadArtifactRequest,
        context: RequestContext = RequestContext(),
    ) -> ManagedWriteResult:
        await self._require_create(CatalogOperation.UPLOAD, request.path, request.artifact_id, context)
        return await self._writer.upload(request, context)

    async def promote(
        self,
        request: PromoteOutputRequest,
        context: RequestContext = RequestContext(),
    ) -> ManagedWriteResult:
        await self._require_create(CatalogOperation.PROMOTE, request.path, request.artifact_id, context)
        return await self._writer.promote(request, context)

    async def remove(
        self,
        request: RemoveArtifactRequest,
        context: RequestContext = RequestContext(),
    ) -> ManagedWriteResult:
        await self._require(CatalogOperation.REMOVE, context)
        reference = ArtifactReference(
            request.reference.artifact_id,
            self._writer.source_id,
            request.reference.revision,
        )
        await self._require(CatalogOperation.REMOVE, context, reference)
        return await self._writer.remove(request, context)


def managed_writer_extension_factory(writer: ManagedCatalogWriter):
    """Adapt a managed writer to ``CatalogIntegration.capability_extension_factory``."""

    def create(_session: object, catalog: object, _context: RequestContext) -> AuthorizedManagedCatalogWriter:
        authorizer = getattr(catalog, "authorizer", None)
        if authorizer is None:
            raise TypeError("Managed writer integration requires an authorized catalog.")
        return AuthorizedManagedCatalogWriter(writer, authorizer)

    return create


__all__ = [
    "ArtifactMetadata",
    "AuthorizedManagedCatalogWriter",
    "BlobManagedStorage",
    "DeleteOutcome",
    "LocalManagedStorage",
    "ManagedCatalogWriter",
    "ManagedWriteResult",
    "PromoteOutputRequest",
    "ReconciliationReport",
    "RegisterArtifactRequest",
    "RemoveArtifactRequest",
    "UploadArtifactRequest",
    "managed_writer_extension_factory",
]
