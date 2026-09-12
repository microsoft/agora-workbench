"""
Asset publishers for pushing artifacts to remote storage destinations.

Each publisher handles artifact delivery for a specific storage backend
(Blob, local filesystem, etc.) and is the symmetric counterpart to
``AssetFetcher``.

Tag-based routing mirrors the fetcher pattern:
  - Fetching: agent passes ``<blob>abc123</blob>`` → ``BlobFetcher.can_handle()``
  - Publishing: agent passes ``<blob>results.csv</blob>`` → ``BlobPublisher.can_handle()``

Authentication:
    Publishers accept an ``AsyncTokenCredential`` (from ``azure.core``) which
    provides tokens for downstream Azure resources. In production this is
    typically backed by managed identity.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import inspect
import logging
import os
import re
import secrets
import stat
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from agora_workbench.data_lake.errors import (
    TransferChecksumError,
    TransferTimeoutError,
    UnsupportedOperationError,
    UnsafePathError,
)
from agora_workbench.data_lake.identity import (
    AzureBlobScope,
    RESERVED_PROVIDER_PREFIX,
    azure_uri_from_blob_name,
    normalize_logical_path,
    parse_azure_uri,
    validate_managed_revision_path,
    validate_azure_object_path,
)
from agora_workbench.data_lake.models import RequestContext
from agora_workbench.data_lake.transfer import (
    TransferDiagnostic,
    TransferOptions,
    TransferResult,
    _run_blocking_io,
    await_transfer,
    check_transfer_cancelled,
    check_transfer_size,
    emit_transfer_diagnostic,
    safe_transfer_resource,
    stream_chunks_to_file,
)

if TYPE_CHECKING:
    from azure.core.credentials_async import AsyncTokenCredential

LOGGER = logging.getLogger(__name__)
_USE_POSIX_DIR_FDS = os.name == "posix"


class ObjectTransferError(RuntimeError):
    """Actionable error returned by a peer server during object transfer."""

    def __init__(self, server_name: str, status_code: int, response_body: dict[str, Any]):
        receiver_error = response_body.get("error")
        if not isinstance(receiver_error, str) or not receiver_error.strip():
            receiver_error = f"Peer returned HTTP {status_code}"

        self.server_name = server_name
        self.status_code = status_code
        self.response_body = response_body
        super().__init__(f"Object transfer to '{server_name}' failed: {receiver_error}")

    def to_payload(self) -> dict[str, Any]:
        """Return the peer response as an agent-facing send-tool error."""
        payload = dict(self.response_body)
        payload["success"] = False
        payload["error"] = str(self)
        payload["status_code"] = self.status_code
        return payload


def _validate_artifact_name(name: str, *, allow_reserved: bool = False) -> None:
    """Validate that an artifact name is safe for path construction.

    Rejects absolute paths, parent-directory traversal (``..``), and empty
    names to prevent writes outside the intended session directory.

    Args:
        name: The logical artifact name extracted from a destination tag.

    Raises:
        ValueError: If the name is unsafe.
    """
    if not name or not name.strip():
        raise ValueError("Artifact name must not be empty.")
    if Path(name).is_absolute():
        raise ValueError(f"Artifact name must not be an absolute path: {name!r}")
    # Check for '..' in any path segment after normalization.
    normalized = name.replace("\\", "/")
    if ".." in Path(normalized).parts:
        raise ValueError(f"Artifact name must not contain parent traversal (..): {name!r}")
    normalized = normalize_logical_path(normalized)
    if not allow_reserved and (
        normalized == RESERVED_PROVIDER_PREFIX.rstrip("/") or normalized.startswith(RESERVED_PROVIDER_PREFIX)
    ):
        raise ValueError("Artifact name is reserved for provider metadata.")


def _open_posix_path_no_follow(path: Path, *, directory: bool = False) -> int:
    """Open an absolute or relative path without following any symlink component."""
    absolute_path = Path(os.path.abspath(os.fspath(path)))
    current = os.open(
        os.path.sep,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        for index, part in enumerate(absolute_path.parts[1:]):
            is_final = index == len(absolute_path.parts) - 2
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            if not is_final or directory:
                flags |= getattr(os, "O_DIRECTORY", 0)
            next_fd = os.open(part, flags, dir_fd=current)
            os.close(current)
            current = next_fd
        return current
    except BaseException:
        os.close(current)
        raise


def _open_or_create_posix_directory(path: Path) -> int:
    """Create and open a directory through no-follow descriptor traversal."""
    absolute_path = Path(os.path.abspath(os.fspath(path)))
    current = os.open(
        os.path.sep,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        for part in absolute_path.parts[1:]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=current)
            except FileExistsError:
                LOGGER.debug("Secure directory component already exists: %s", part)
            next_fd = os.open(
                part,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=current,
            )
            os.close(current)
            current = next_fd
        return current
    except BaseException:
        os.close(current)
        raise


def _validate_publish_path(path: str, *, allow_reserved: bool) -> str:
    """Validate an ordinary path or the narrow trusted managed-revision seam."""
    if allow_reserved:
        return validate_managed_revision_path(path)
    return validate_azure_object_path(path)


# Regex for parsing tag-based destination strings.
# Matches both closed (``<blob>name</blob>``) and unclosed (``<blob>name``)
# forms to tolerate LLM output that occasionally omits the closing tag.
_TAG_RE = re.compile(r"^<(\w+)>([^<>]+?)(?:</\1>)?$")


def parse_destination_tag(destination: str) -> tuple[str, str] | None:
    """Parse a tag-based destination string into (tag_type, name).

    Accepts both ``<blob>results.csv</blob>`` and ``<blob>results.csv``
    (unclosed-tag fallback for LLM robustness).

    Args:
        destination: The tagged destination string from the agent.

    Returns:
        ``(tag_type, name)`` tuple, or ``None`` if the string is not
        a recognised tag format.
    """
    m = _TAG_RE.match(destination.strip())
    if m:
        return m.group(1), m.group(2)
    return None


def _inspect_publish_capabilities(implementation: Any) -> tuple[bool, bool, bool]:
    """Return keyword capabilities for one publisher implementation."""
    try:
        signature = inspect.signature(implementation)
    except (TypeError, ValueError):
        return False, False, False
    supports_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values()
    )
    return supports_kwargs, "options" in signature.parameters, "context" in signature.parameters


@functools.lru_cache(maxsize=128)
def _publish_capabilities(implementation: Any) -> tuple[bool, bool, bool]:
    """Return cached keyword capabilities for a stable publisher implementation."""
    return _inspect_publish_capabilities(implementation)


async def publish_compat(
    publisher: Any,
    *,
    local_path: Path,
    name: str,
    session_id: str,
    options: TransferOptions | None = None,
    context: RequestContext | None = None,
) -> str:
    """Call modern or legacy publishers without masking implementation errors."""
    publish = publisher.publish
    implementation = getattr(publish, "__func__", None)
    if implementation is not None and implementation is getattr(type(publisher), "publish", None):
        supports_kwargs, supports_options, supports_context = _publish_capabilities(implementation)
    else:
        supports_kwargs, supports_options, supports_context = _inspect_publish_capabilities(publish)
    kwargs: dict[str, object] = {
        "local_path": local_path,
        "name": name,
        "session_id": session_id,
    }
    if supports_kwargs or supports_options:
        kwargs["options"] = options
    if supports_kwargs or supports_context:
        kwargs["context"] = context
    return await publish(**kwargs)


async def _copy_local_descriptors(
    local_path: Path,
    output_fd: int,
    options: TransferOptions,
    context: RequestContext,
) -> TransferResult:
    """Copy a regular source descriptor to an already-secured destination descriptor."""
    source_fd = (
        _open_posix_path_no_follow(local_path)
        if _USE_POSIX_DIR_FDS
        else os.open(local_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    )
    started = time.monotonic()
    total = 0
    digest = hashlib.sha256()

    async def copy() -> None:
        nonlocal total
        if not stat.S_ISREG(os.fstat(source_fd).st_mode):
            raise UnsafePathError("Upload source must be a regular file.", operation="upload")
        check_transfer_size(os.fstat(source_fd).st_size, options, operation="upload", resource=str(local_path))
        while True:
            check_transfer_cancelled(options, operation="upload", resource=str(local_path))
            chunk = await _run_blocking_io(
                lambda: os.read(source_fd, options.chunk_size),
                options=options,
                operation="upload",
                resource=str(local_path),
            )
            if not chunk:
                break
            total += len(chunk)
            check_transfer_size(total, options, operation="upload", resource=str(local_path))
            digest.update(chunk)
            remaining = memoryview(chunk)
            while remaining:
                written = await _run_blocking_io(
                    lambda: os.write(output_fd, remaining),
                    options=options,
                    operation="upload",
                    resource=str(local_path),
                )
                if written <= 0:
                    raise OSError("Transfer output made no write progress.")
                remaining = remaining[written:]
        await _run_blocking_io(
            lambda: os.fsync(output_fd),
            options=options,
            operation="upload",
            resource=str(local_path),
        )

    try:
        if options.timeout_seconds is None:
            await copy()
        else:
            async with asyncio.timeout(options.timeout_seconds):
                await copy()
    except TimeoutError as exc:
        message = (
            "Provider transfer timed out."
            if options.timeout_seconds is None
            else f"Transfer exceeded the configured {options.timeout_seconds:g}-second timeout."
        )
        raise TransferTimeoutError(
            message,
            resource_id=str(local_path),
            operation="upload",
        ) from exc
    finally:
        os.close(source_fd)
    checksum = digest.hexdigest()
    if options.expected_sha256 is not None and not secrets.compare_digest(checksum, options.expected_sha256):
        raise TransferChecksumError(
            "Transfer checksum did not match the expected SHA-256 digest.",
            resource_id=str(local_path),
            operation="upload",
        )
    return TransferResult(total, checksum, context, str(local_path), time.monotonic() - started)


async def _copy_local_path(
    local_path: Path,
    destination: Path,
    options: TransferOptions,
    context: RequestContext,
) -> TransferResult:
    """Best-effort fallback for unrestricted local publishing on non-POSIX platforms."""

    def open_verified_source():
        source_path = local_path.resolve(strict=True)
        source_stat = source_path.stat()
        source_file = source_path.open("rb", buffering=0)
        opened_stat = os.fstat(source_file.fileno())
        if (opened_stat.st_dev, opened_stat.st_ino) != (source_stat.st_dev, source_stat.st_ino):
            source_file.close()
            raise UnsafePathError("Upload source identity changed before open.", operation="upload")
        return source_path, source_file

    source_path, source_file = await _run_blocking_io(
        open_verified_source,
        options=options,
        operation="upload",
        resource=str(local_path),
    )
    try:

        async def chunks():
            while True:
                chunk = await _run_blocking_io(
                    lambda: source_file.read(options.chunk_size),
                    options=options,
                    operation="upload",
                    resource=str(source_path),
                )
                if not chunk:
                    break
                yield chunk

        return await stream_chunks_to_file(
            chunks(),
            destination,
            options=options,
            context=context,
            operation="upload",
            resource=str(source_path),
        )
    finally:
        await _run_blocking_io(source_file.close)


class AssetPublisher(ABC):
    """Base class for artifact publishers.

    Publishers are the symmetric counterpart to :class:`~.fetchers.AssetFetcher`:
    they push a local file produced by an agent session to a remote storage
    destination.

    Concrete implementations are configured at server startup and registered
    with the :class:`~code_execution.server.CodeExecutionServer`.  Operators
    control which destinations are reachable by which publishers they register —
    no separate allowlist environment variable is needed.
    """

    def __init__(self, credential: "AsyncTokenCredential | None" = None):
        """
        Initialise the publisher with an optional async token credential.

        Args:
            credential: An ``AsyncTokenCredential`` that provides tokens for
                downstream Azure resources (e.g. ``ManagedIdentityCredential``).
                May be ``None`` for publishers that don't require credentials
                (e.g. local filesystem).
        """
        self.credential = credential

    @property
    @abstractmethod
    def destination_name(self) -> str:
        """Logical name used for routing in the unified send tool.

        This name is what agents pass as the ``to`` parameter, e.g.
        ``"blob"``, ``"user"``, ``"gis"``.  Must be unique across all
        publishers registered on a single server.
        """
        raise NotImplementedError

    @abstractmethod
    async def publish(
        self,
        local_path: Path,
        name: str,
        session_id: str,
        *,
        options: TransferOptions | None = None,
        context: RequestContext | None = None,
    ) -> str:
        """Publish a local artifact to this publisher's configured destination.

        The publisher owns path placement logic — it combines its configured
        base path with the session context and the logical name to derive the
        full destination path.

        Args:
            local_path: Absolute path to the file to publish.
            name: Logical name (relative path-like value from the tag inner
                text, e.g. ``"results.csv"`` or ``"subdir/report.pdf"``).
            session_id: Active session ID used to scope the upload path.

        Returns:
            The remote URI of the published artifact (e.g.
            ``"https://account.blob.core.windows.net/container/session/name"``
            or ``"/mnt/shared/outputs/session/name"``).
        """
        raise NotImplementedError

    async def publish_with_result(
        self,
        local_path: Path,
        name: str,
        session_id: str,
        *,
        options: TransferOptions | None = None,
        context: RequestContext | None = None,
    ) -> tuple[str, TransferResult]:
        """Publish with detailed transfer data when supported by the provider."""
        raise UnsupportedOperationError(
            f"{type(self).__name__} does not implement bounded file publishing.",
            operation="upload",
        )

    @abstractmethod
    def can_handle(self, destination: str) -> bool:
        """Check whether this publisher handles the given tagged destination.

        Args:
            destination: Tagged destination string, e.g. ``"<blob>results.csv</blob>"``.

        Returns:
            ``True`` if this publisher accepts the tag type.
        """
        raise NotImplementedError

    async def close(self) -> None:
        """Release any resources held by this publisher.

        The default implementation is a no-op; override when the publisher
        holds pooled connections or clients that need explicit teardown.
        """


class BlobPublisher(AssetPublisher):
    """Publisher that uploads artifacts to Azure Blob Storage.

    Configured at startup with a storage account URL and container name.
    Files are placed at ``{container}/{session_id}/{name}`` inside the
    configured account.

    Maintains a per-account ``BlobServiceClient`` cache to amortise TCP/TLS
    handshake and token acquisition costs across multiple publishes.

    Handles destination tags of the form ``<blob>name</blob>``.
    """

    # Azure Storage scope for token acquisition
    STORAGE_SCOPE = "https://storage.azure.com/.default"

    @property
    def destination_name(self) -> str:  # noqa: D102
        return "blob"

    def __init__(
        self,
        account_url: str,
        container: str,
        credential: "AsyncTokenCredential | None" = None,
        *,
        prefix: str = "",
        staging_dir: Path | str | None = None,
    ):
        """
        Initialise the BlobPublisher.

        Args:
            account_url: Azure Storage account URL, e.g.
                ``"https://myaccount.blob.core.windows.net"``.
            container: Container name to upload into.
            credential: An ``AsyncTokenCredential`` for blob auth (typically
                managed identity).  Reuse the same credential instance as the
                server's :class:`~.fetchers.BlobFetcher` to avoid redundant
                token refreshes.
        """
        super().__init__(credential=credential)
        parsed = urlsplit(account_url)
        if (
            parsed.scheme.lower() != "https"
            or parsed.username is not None
            or parsed.password is not None
            or "@" in parsed.netloc
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("BlobPublisher account_url must be a credential-free Azure HTTPS account URL.")
        account, _, _ = parse_azure_uri(f"{account_url.rstrip('/')}/container")
        scope = AzureBlobScope(account, container, prefix)
        self._account_url = f"https://{scope.account}.blob.core.windows.net"
        self._container = scope.container
        self._prefix = scope.prefix
        configured_staging = staging_dir or (
            Path(os.getenv("MCP_ASSET_CACHE_DIR", os.getcwd())) / ".agora-transfer-staging"
        )
        self._staging_dir = Path(os.path.abspath(os.fspath(configured_staging)))
        self._staging_fd: int | None = None
        self._staging_identity: tuple[int, int] | None = None
        if _USE_POSIX_DIR_FDS:
            self._staging_fd = _open_or_create_posix_directory(self._staging_dir)
            stat_result = os.fstat(self._staging_fd)
            self._staging_identity = (stat_result.st_dev, stat_result.st_ino)
        self._client = None  # lazily initialised

    def _get_client(self):
        """Return (or lazily create) the BlobServiceClient."""
        if self._client is None:
            from azure.storage.blob.aio import BlobServiceClient

            self._client = BlobServiceClient(
                account_url=self._account_url,
                credential=self.credential,
            )
        return self._client

    async def close(self) -> None:
        """Close the underlying BlobServiceClient."""
        try:
            if self._client is not None:
                await self._client.close()
                self._client = None
        finally:
            if self._staging_fd is not None:
                os.close(self._staging_fd)
                self._staging_fd = None

    def _open_verified_staging_root(self) -> int:
        """Return the retained staging root after verifying its configured identity."""
        if not _USE_POSIX_DIR_FDS:
            self._staging_dir.mkdir(parents=True, exist_ok=True)
            return -1
        if self._staging_fd is None or self._staging_identity is None:
            raise UnsafePathError("Blob publisher staging root is unavailable.", operation="upload")
        current_fd = _open_posix_path_no_follow(self._staging_dir, directory=True)
        try:
            stat_result = os.fstat(current_fd)
            if (stat_result.st_dev, stat_result.st_ino) != self._staging_identity:
                raise UnsafePathError(
                    "Blob publisher staging root was replaced after configuration.", operation="upload"
                )
        finally:
            os.close(current_fd)
        return os.dup(self._staging_fd)

    def can_handle(self, destination: str) -> bool:
        """Return ``True`` for ``<blob>…</blob>`` destinations."""
        parsed = parse_destination_tag(destination)
        return parsed is not None and parsed[0] == "blob"

    async def publish(
        self,
        local_path: Path,
        name: str,
        session_id: str,
        *,
        options: TransferOptions | None = None,
        context: RequestContext | None = None,
    ) -> str:
        """Upload *local_path* to ``{container}/{session_id}/{name}``.

        Args:
            local_path: Absolute path to the file to upload.
            name: Logical name / relative path within the session's namespace.
            session_id: Session ID used to scope the blob path.

        Returns:
            The full HTTPS URL of the uploaded blob.

        Raises:
            FileNotFoundError: If *local_path* does not exist.
            azure.core.exceptions.ClientAuthenticationError: If the credential
                is not authorised to write to the container.
        """
        remote_uri, _ = await self.publish_with_result(
            local_path,
            name,
            session_id,
            options=options,
            context=context,
        )
        return remote_uri

    async def publish_with_result(
        self,
        local_path: Path,
        name: str,
        session_id: str,
        *,
        options: TransferOptions | None = None,
        context: RequestContext | None = None,
    ) -> tuple[str, TransferResult]:
        """Upload a regular file with bounded reads, timeout, cancellation, and checksum."""
        options = options or TransferOptions()
        context = context or RequestContext()
        if not local_path.is_file():
            raise FileNotFoundError(f"Artifact not found at {local_path}")
        _validate_artifact_name(name, allow_reserved=options.allow_reserved)
        if session_id:
            _validate_artifact_name(session_id)
        relative_path = "/".join(part for part in (self._prefix, session_id, name) if part)
        blob_path = _validate_publish_path(relative_path, allow_reserved=options.allow_reserved)
        remote_uri = azure_uri_from_blob_name(
            parse_azure_uri(f"{self._account_url}/{self._container}")[0],
            self._container,
            blob_path,
        )
        display_uri = safe_transfer_resource(remote_uri)
        LOGGER.info(
            "BlobPublisher: uploading %s → %s/%s/%s",
            local_path,
            self._account_url,
            self._container,
            blob_path,
        )

        client = self._get_client()
        blob_client = client.get_blob_client(container=self._container, blob=blob_path)
        started = time.monotonic()
        await emit_transfer_diagnostic(options, TransferDiagnostic("upload", "started", context, display_uri))
        snapshot_name = f"{secrets.token_hex(16)}.upload"
        snapshot_path = self._staging_dir / snapshot_name
        staging_fd: int | None = None
        snapshot_fd: int | None = None
        snapshot_created = False
        try:
            staging_fd = self._open_verified_staging_root()
            if staging_fd == -1:
                snapshot_fd = os.open(
                    snapshot_path,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                snapshot_created = True
            else:
                snapshot_fd = os.open(
                    snapshot_name,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=staging_fd,
                )
                snapshot_created = True

            async def perform_upload() -> TransferResult:
                snapshot = await _copy_local_descriptors(local_path, snapshot_fd, options, context)
                os.lseek(snapshot_fd, 0, os.SEEK_SET)
                check_transfer_cancelled(options, operation="upload", resource=display_uri)
                metadata = dict(options.object_metadata)
                with os.fdopen(os.dup(snapshot_fd), "rb", closefd=True) as source:
                    if options.create_exclusive and metadata:
                        upload = blob_client.upload_blob(
                            source,
                            overwrite=False,
                            if_none_match="*",
                            metadata=metadata,
                        )
                    elif options.create_exclusive:
                        upload = blob_client.upload_blob(
                            source,
                            overwrite=False,
                            if_none_match="*",
                        )
                    elif metadata:
                        upload = blob_client.upload_blob(
                            source,
                            overwrite=True,
                            metadata=metadata,
                        )
                    else:
                        upload = blob_client.upload_blob(source, overwrite=True)
                    await await_transfer(
                        upload,
                        TransferOptions(
                            max_bytes=options.max_bytes,
                            quota_bytes=options.quota_bytes,
                            timeout_seconds=None,
                            chunk_size=options.chunk_size,
                            cancellation_event=options.cancellation_event,
                        ),
                        operation="upload",
                        resource=display_uri,
                    )
                return snapshot

            try:
                if options.timeout_seconds is None:
                    uploaded = await perform_upload()
                else:
                    async with asyncio.timeout(options.timeout_seconds):
                        uploaded = await perform_upload()
            except TimeoutError as exc:
                message = (
                    "Provider transfer timed out."
                    if options.timeout_seconds is None
                    else f"Transfer exceeded the configured {options.timeout_seconds:g}-second timeout."
                )
                raise TransferTimeoutError(
                    message,
                    resource_id=display_uri,
                    operation="upload",
                ) from exc
        except BaseException as exc:
            await emit_transfer_diagnostic(
                options,
                TransferDiagnostic("upload", "failed", context, display_uri, error_type=type(exc).__name__),
            )
            raise
        finally:
            if snapshot_fd is not None:
                os.close(snapshot_fd)
            if snapshot_created and (staging_fd is None or staging_fd == -1):
                snapshot_path.unlink(missing_ok=True)
            elif snapshot_created and staging_fd is not None:
                try:
                    os.unlink(snapshot_name, dir_fd=staging_fd)
                except FileNotFoundError:
                    LOGGER.debug("BlobPublisher snapshot was already removed: %s", snapshot_name)
            if staging_fd is not None and staging_fd != -1:
                os.close(staging_fd)
        result = TransferResult(
            uploaded.bytes_transferred,
            uploaded.checksum_sha256,
            context,
            display_uri,
            time.monotonic() - started,
            True if options.create_exclusive else None,
            options.object_metadata,
        )
        await emit_transfer_diagnostic(
            options,
            TransferDiagnostic(
                "upload",
                "completed",
                context,
                display_uri,
                result.bytes_transferred,
                result.checksum_sha256,
            ),
        )
        LOGGER.info("BlobPublisher: uploaded %d bytes → %s", result.bytes_transferred, display_uri)
        return f"{self._account_url}{urlsplit(remote_uri).path}", result


class GuiPublisher(AssetPublisher):
    """Publisher that exposes artifacts via the server's download endpoint.

    Unlike Blob or Local publishers that transfer the file elsewhere, this
    publisher simply returns the server's ``/artifacts/`` download URL for
    the already-registered artifact.  The activity UI surfaces this URL so
    the user can download the file from their browser.

    This publisher is auto-registered on every ``CodeExecutionServer`` so
    agents can always use ``<gui>filename</gui>`` to make outputs
    downloadable without requiring operator-configured storage backends.

    Handles destination tags of the form ``<gui>name</gui>``.
    """

    @property
    def destination_name(self) -> str:  # noqa: D102
        return "user"

    def __init__(self, public_url_fn: "Callable[[], str]"):
        """
        Initialise the GuiPublisher.

        Args:
            public_url_fn: A callable returning the server's public base URL
                (e.g. ``server.public_url``).  Deferred so the URL reflects
                the actual bind address after ``run_http()`` is called.
        """
        super().__init__(credential=None)
        self._public_url_fn = public_url_fn

    def can_handle(self, destination: str) -> bool:
        """Return ``True`` for ``<gui>…</gui>`` destinations."""
        parsed = parse_destination_tag(destination)
        return parsed is not None and parsed[0] == "gui"

    async def publish(
        self,
        local_path: Path,
        name: str,
        session_id: str,
        *,
        options: TransferOptions | None = None,
        context: RequestContext | None = None,
    ) -> str:
        """Return the download URL for the artifact.

        The artifact must already be registered in the session manager's
        artifact registry (populated by the snapshot-diff after execution).
        The caller (the publish tool wrapper) is responsible for passing the
        download token via the ``_download_token`` attribute set on this
        instance before calling ``publish()``.

        Args:
            local_path: Absolute path to the artifact file.
            name: Logical name / relative path for the URL's filename segment.
            session_id: Session ID scoping the artifact.

        Returns:
            The fully-qualified download URL.

        Raises:
            FileNotFoundError: If *local_path* does not exist.
            RuntimeError: If no download token was provided.
        """
        del options, context
        if not local_path.is_file():
            raise FileNotFoundError(f"Artifact not found at {local_path}")

        _validate_artifact_name(name)

        token = getattr(self, "_download_token", None)
        if not token:
            raise RuntimeError(
                "GuiPublisher requires a download token set via _download_token before publish() is called."
            )

        public_base = (os.getenv("SERVER_PUBLIC_URL") or self._public_url_fn()).rstrip("/")
        download_url = f"{public_base}/artifacts/{session_id}/{token}/{name}"
        LOGGER.info("GuiPublisher: exposing %s for session %s as %s", local_path, session_id, name)
        return download_url


class LocalFilePublisher(AssetPublisher):
    """Publisher that copies artifacts to a local directory.

    Configured at startup with a base directory.  Files are placed at
    ``{base_dir}/{session_id}/{name}``.

    Handles destination tags of the form ``<local>name</local>``.
    """

    @property
    def destination_name(self) -> str:  # noqa: D102
        return "local"

    def __init__(self, base_dir: Path | str):
        """
        Initialise the LocalFilePublisher.

        Args:
            base_dir: Base directory under which session sub-directories are
                created.  Must be an absolute path (or will be resolved to
                one).
        """
        super().__init__(credential=None)
        self._base_dir = Path(os.path.abspath(os.fspath(base_dir)))
        self._anchor_fd: int | None = None
        self._root_parts: tuple[str, ...] = ()
        self._root_identity: tuple[int, int] | None = None
        self._portable_anchor_path: Path | None = None
        self._portable_anchor_identity: tuple[int, int] | None = None
        if _USE_POSIX_DIR_FDS:
            self._initialize_root_anchor()
        else:
            self._initialize_portable_root()

    def _initialize_portable_root(self) -> None:
        """Capture the existing root or nearest ancestor for best-effort non-POSIX validation."""
        candidate = self._base_dir
        while not candidate.exists():
            if candidate == candidate.parent:
                raise FileNotFoundError(f"No existing ancestor for local publisher root: {self._base_dir}")
            candidate = candidate.parent
        if candidate.is_symlink() or not candidate.is_dir():
            raise UnsafePathError("Local publisher root ancestor must be a real directory.", operation="upload")
        resolved_anchor = candidate.resolve(strict=True)
        anchor_stat = resolved_anchor.stat()
        self._portable_anchor_path = resolved_anchor
        self._portable_anchor_identity = (anchor_stat.st_dev, anchor_stat.st_ino)
        if self._base_dir.exists():
            resolved_root = self._base_dir.resolve(strict=True)
            if resolved_root != self._base_dir:
                raise UnsafePathError("Local publisher root must not be a symlink.", operation="upload")
            root_stat = resolved_root.stat()
            self._root_identity = (root_stat.st_dev, root_stat.st_ino)

    def _verify_portable_root(self) -> Path:
        """Resolve the non-POSIX root while checking identities captured at construction."""
        if self._portable_anchor_path is None or self._portable_anchor_identity is None:
            raise UnsafePathError("Local publisher root is unavailable.", operation="upload")
        current_anchor = self._portable_anchor_path.resolve(strict=True)
        anchor_stat = current_anchor.stat()
        if (
            current_anchor != self._portable_anchor_path
            or (anchor_stat.st_dev, anchor_stat.st_ino) != self._portable_anchor_identity
        ):
            raise UnsafePathError("Local publisher root ancestor identity changed.", operation="upload")
        self._base_dir.mkdir(parents=True, exist_ok=True)
        resolved_root = self._base_dir.resolve(strict=True)
        if not resolved_root.is_relative_to(self._portable_anchor_path):
            raise UnsafePathError("Local publisher root escapes its configured ancestor.", operation="upload")
        root_stat = resolved_root.stat()
        identity = (root_stat.st_dev, root_stat.st_ino)
        if self._root_identity is None:
            self._root_identity = identity
        elif identity != self._root_identity:
            raise UnsafePathError("Local publisher root was replaced after configuration.", operation="upload")
        return resolved_root

    def _initialize_root_anchor(self) -> None:
        """Retain a trusted ancestor descriptor for no-follow root traversal."""
        candidate = self._base_dir if self._base_dir == self._base_dir.parent else self._base_dir.parent
        while True:
            try:
                candidate.lstat()
                break
            except FileNotFoundError:
                if candidate == candidate.parent:
                    raise
                candidate = candidate.parent
        if candidate.is_symlink() or not candidate.is_dir():
            raise UnsafePathError("Local publisher root ancestor must be a real directory.", operation="upload")
        self._anchor_fd = _open_posix_path_no_follow(candidate, directory=True)
        relative_existing = self._base_dir.relative_to(candidate).parts
        self._root_parts = tuple(relative_existing) if relative_existing else ()
        if not self._root_parts:
            stat_result = os.fstat(self._anchor_fd)
            self._root_identity = (stat_result.st_dev, stat_result.st_ino)
            return
        try:
            self._base_dir.lstat()
        except FileNotFoundError:
            return
        root_fd = _open_posix_path_no_follow(self._base_dir, directory=True)
        try:
            stat_result = os.fstat(root_fd)
            self._root_identity = (stat_result.st_dev, stat_result.st_ino)
        finally:
            os.close(root_fd)

    def _open_verified_root(self) -> int:
        """Open/create the configured root beneath the retained trusted ancestor."""
        if self._anchor_fd is None:
            raise UnsafePathError("Local publisher root is unavailable.", operation="upload")
        current = os.dup(self._anchor_fd)
        try:
            for part in self._root_parts:
                try:
                    os.mkdir(part, mode=0o750, dir_fd=current)
                except FileExistsError:
                    LOGGER.debug("Local publisher root component already exists: %s", part)
                next_fd = os.open(
                    part,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=current,
                )
                os.close(current)
                current = next_fd
            stat_result = os.fstat(current)
            identity = (stat_result.st_dev, stat_result.st_ino)
            if self._root_identity is None:
                self._root_identity = identity
            elif identity != self._root_identity:
                raise UnsafePathError("Local publisher root was replaced after configuration.", operation="upload")
            return current
        except BaseException:
            os.close(current)
            raise

    async def close(self) -> None:
        """Close the retained trusted root descriptor."""
        if self._anchor_fd is not None:
            os.close(self._anchor_fd)
            self._anchor_fd = None

    def can_handle(self, destination: str) -> bool:
        """Return ``True`` for ``<local>…</local>`` destinations."""
        parsed = parse_destination_tag(destination)
        return parsed is not None and parsed[0] == "local"

    async def publish(
        self,
        local_path: Path,
        name: str,
        session_id: str,
        *,
        options: TransferOptions | None = None,
        context: RequestContext | None = None,
    ) -> str:
        """Copy *local_path* to ``{base_dir}/{session_id}/{name}``.

        Args:
            local_path: Absolute path to the file to copy.
            name: Logical name / relative path within the session's namespace.
            session_id: Session ID used to scope the destination path.

        Returns:
            The absolute path of the copied file as a string.

        Raises:
            FileNotFoundError: If *local_path* does not exist.
        """
        destination, _ = await self.publish_with_result(
            local_path,
            name,
            session_id,
            options=options,
            context=context,
        )
        return destination

    async def publish_with_result(
        self,
        local_path: Path,
        name: str,
        session_id: str,
        *,
        options: TransferOptions | None = None,
        context: RequestContext | None = None,
    ) -> tuple[str, TransferResult]:
        """Copy into the configured root without following destination symlinks."""
        options = options or TransferOptions()
        context = context or RequestContext()
        if not local_path.is_file():
            raise FileNotFoundError(f"Artifact not found at {local_path}")
        _validate_artifact_name(name, allow_reserved=options.allow_reserved)
        if session_id:
            _validate_artifact_name(session_id)
        if options.object_metadata:
            raise UnsupportedOperationError(
                "LocalFilePublisher does not support object metadata.",
                operation="upload",
            )
        relative_text = normalize_logical_path("/".join(part for part in (session_id, name) if part))
        _validate_publish_path(relative_text, allow_reserved=options.allow_reserved)
        relative = Path(relative_text)
        destination = self._base_dir / relative
        started = time.monotonic()
        await emit_transfer_diagnostic(
            options,
            TransferDiagnostic("upload", "started", context, str(destination)),
        )
        try:
            result = await self._copy_secure(local_path, relative, options, context)
        except BaseException as exc:
            await emit_transfer_diagnostic(
                options,
                TransferDiagnostic("upload", "failed", context, str(destination), error_type=type(exc).__name__),
            )
            raise
        result = TransferResult(
            result.bytes_transferred,
            result.checksum_sha256,
            context,
            str(destination),
            time.monotonic() - started,
            result.created,
            result.object_metadata,
        )
        await emit_transfer_diagnostic(
            options,
            TransferDiagnostic(
                "upload",
                "completed",
                context,
                str(destination),
                result.bytes_transferred,
                result.checksum_sha256,
            ),
        )
        LOGGER.info("LocalFilePublisher: copied %d bytes → %s", result.bytes_transferred, destination)
        return str(destination), result

    async def _copy_secure(
        self,
        local_path: Path,
        relative: Path,
        options: TransferOptions,
        context: RequestContext,
    ) -> TransferResult:
        if not _USE_POSIX_DIR_FDS:
            resolved_base = self._verify_portable_root()
            destination = (resolved_base / relative).resolve()
            if not destination.is_relative_to(resolved_base):
                raise UnsafePathError("Local publish path escapes the configured root.", operation="upload")
            destination.parent.mkdir(parents=True, exist_ok=True)
            return await _copy_local_path(local_path, destination, options, context)

        root_fd = self._open_verified_root()
        parent_fd = root_fd
        temporary_name = f".{relative.name}.{secrets.token_hex(8)}.part"
        output_fd: int | None = None
        committed = False
        try:
            for part in relative.parts[:-1]:
                try:
                    os.mkdir(part, mode=0o750, dir_fd=parent_fd)
                except FileExistsError:
                    LOGGER.debug("Local publisher destination directory already exists: %s", part)
                next_fd = os.open(
                    part,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=parent_fd,
                )
                if parent_fd != root_fd:
                    os.close(parent_fd)
                parent_fd = next_fd
            output_fd = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o640,
                dir_fd=parent_fd,
            )
            result = await _copy_local_descriptors(local_path, output_fd, options, context)
            os.close(output_fd)
            output_fd = None
            check_transfer_cancelled(options, operation="upload", resource=str(local_path))
            if options.create_exclusive:
                os.link(
                    temporary_name,
                    relative.name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                # Linking the validated bytes under the final name commits the
                # publish; removal of the temporary name is best-effort.
                committed = True
                try:
                    os.unlink(temporary_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    # The final link is committed and no temporary name remains.
                    pass
                except OSError:
                    LOGGER.warning(
                        "Could not remove committed local publish temporary file %s.",
                        temporary_name,
                        exc_info=True,
                    )
            else:
                os.replace(temporary_name, relative.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                committed = True
            return TransferResult(
                result.bytes_transferred,
                result.checksum_sha256,
                result.context,
                result.resource,
                result.elapsed_seconds,
                True if options.create_exclusive else None,
                options.object_metadata,
            )
        finally:
            if output_fd is not None:
                os.close(output_fd)
            if not committed:
                try:
                    os.unlink(temporary_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    # A failed operation may already have removed its temporary file.
                    pass
            if parent_fd != root_fd:
                os.close(parent_fd)
            os.close(root_fd)


class ServerPublisher(AssetPublisher):
    """Publisher that pushes serialized objects to a peer MCP server's kernel.

    Each instance represents a single peer server destination. Operators
    register one ``ServerPublisher`` per reachable peer in the ``publishers``
    list at server construction time.

    The publisher reads a dill-serialized file, base64-encodes it, and POSTs
    it to the target server's ``/object-transfer/receive`` endpoint. URL
    validation (HTTPS enforcement, host allow-lists) is applied before
    sending credentials.

    .. note::

        When ``target_url`` is not provided, the URL is auto-expanded from
        ``server_name`` using the Docker Compose convention
        (``http://{name}-server:8000``). By default, plain HTTP URLs are
        rejected by ``_validate_target_url`` unless the hostname appears in
        the ``OBJECT_TRANSFER_TRUSTED_HTTP_HOSTS`` environment variable.
        Operators deploying with Docker Compose internal networking should
        set this variable to include the auto-expanded hostnames.

    Session resolution on the target: when ``session_id`` is empty the
    receive endpoint selects the first active session owned by the caller
    (identified by the forwarded bearer token). If no active session exists
    on the target, the receive endpoint returns a 404 — the agent must
    ensure a session exists on the target (e.g. via ``execute_{target}_code``)
    before sending.
    """

    def __init__(
        self,
        server_name: str,
        target_url: str | None = None,
        timeout: float = 60.0,
        trust_http: bool = False,
    ):
        """
        Initialise the ServerPublisher.

        Args:
            server_name: Logical name of the target server (e.g. ``"gis"``).
                Used as ``destination_name`` for routing and for Docker URL
                expansion if ``target_url`` is not provided.
            target_url: Full base URL of the target MCP server.  If ``None``,
                the URL is auto-expanded from ``server_name`` using the Docker
                Compose convention (``http://{name}-server:8000``).
            timeout: HTTP read/write timeout in seconds for the transfer request.
            trust_http: When ``True``, plain-HTTP transfers to this peer are
                permitted without listing the host in
                ``OBJECT_TRANSFER_TRUSTED_HTTP_HOSTS``. Set by the unified send
                tool when the publisher is built on demand from an
                operator-curated peer registry, where the operator already
                chose the scheme in the configured URL.
        """
        super().__init__(credential=None)
        self._server_name = server_name
        self._timeout = timeout
        self._trust_http = trust_http
        if target_url:
            self._target_url = target_url.rstrip("/")
        else:
            hostname = server_name if server_name.endswith("-server") else f"{server_name}-server"
            self._target_url = f"http://{hostname}:8000"

    @property
    def destination_name(self) -> str:  # noqa: D102
        return self._server_name

    def can_handle(self, destination: str) -> bool:
        """ServerPublisher does not use tag-based routing.

        It is routed via ``destination_name`` in the unified send tool.
        For backwards-compat with the ``publish_artifact`` tag-based flow,
        this always returns ``False``.
        """
        return False

    async def publish(
        self,
        local_path: Path,
        name: str,
        session_id: str,
        *,
        options: TransferOptions | None = None,
        context: RequestContext | None = None,
    ) -> str:
        """Serialize and push the file to the target server's kernel.

        The file at ``local_path`` is expected to be a dill-serialized pickle.
        It is base64-encoded and sent to the target's ``/object-transfer/receive``
        endpoint.

        Validates the target URL before sending credentials to prevent SSRF and
        bearer-token leakage (see ``_validate_target_url``).

        Args:
            local_path: Path to the serialized (pickle) file to transfer.
            name: Variable name to inject on the target kernel.
            session_id: Session ID on the target server (empty string to
                let the target resolve via bearer token).

        Returns:
            A human-readable confirmation message.

        Raises:
            FileNotFoundError: If *local_path* does not exist.
            RuntimeError: If no user token was set before calling publish.
            ValueError: If the target URL fails validation.
            ObjectTransferError: On structured non-2xx responses from the target.
            httpx.HTTPStatusError: On non-2xx responses from the target.
            httpx.RequestError: On connection / timeout errors.
        """
        import base64
        import re as _re

        import httpx

        from ..object_transfer import _validate_target_url

        del options, context

        if not local_path.is_file():
            raise FileNotFoundError(f"Transfer file not found at {local_path}")

        serialized = local_path.read_bytes()

        # user_token is injected by the send tool before calling publish
        user_token = getattr(self, "_user_token", "")
        if not user_token:
            raise RuntimeError("ServerPublisher requires _user_token to be set before publish() is called.")

        _validate_target_url(self._target_url, trust_http=self._trust_http)

        # Strip common MCP path suffixes so the agent can pass the MCP
        # endpoint URL directly (e.g. http://gis-server:8000/mcp).
        base = _re.sub(r"/mcp/?$", "", self._target_url.rstrip("/"))
        receive_url = f"{base}/object-transfer/receive"

        payload: dict = {
            "variable_name": name,
            "data": base64.b64encode(serialized).decode("ascii"),
            "metadata": {
                "source_server": getattr(self, "_source_server", "unknown"),
                "transfer_id": getattr(self, "_transfer_id", ""),
            },
        }
        if session_id:
            payload["session_id"] = session_id

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=self._timeout, write=self._timeout, pool=10.0),
        ) as client:
            response = await client.post(
                receive_url,
                json=payload,
                headers={"Authorization": f"Bearer {user_token}"},
            )
            try:
                result = response.json()
            except ValueError:
                response.raise_for_status()
                raise

            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if isinstance(result, dict):
                    raise ObjectTransferError(
                        server_name=self._server_name,
                        status_code=response.status_code,
                        response_body=result,
                    ) from exc
                raise

        return f"Injected '{name}' into {self._server_name} kernel (response: {result})"
