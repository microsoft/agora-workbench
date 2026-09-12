"""Bounded transfer contracts shared by data-lake fetchers and publishers."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import os
import re
import secrets
import time
from collections.abc import AsyncIterable, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import BinaryIO
from urllib.parse import urlsplit

from .errors import (
    TransferCancelledError,
    TransferChecksumError,
    TransferLimitError,
    TransferTimeoutError,
)
from .identity import sanitize_uri_for_display
from .models import RequestContext

DEFAULT_TRANSFER_CHUNK_BYTES = 1024 * 1024
DEFAULT_TRANSFER_MAX_BYTES = 1024 * 1024 * 1024
DEFAULT_TRANSFER_TIMEOUT_SECONDS = 300.0
LOGGER = logging.getLogger(__name__)

TransferDiagnosticHook = Callable[["TransferDiagnostic"], Awaitable[None] | None]
_TAGGED_REFERENCE_RE = re.compile(r"^(<[^<>]+>)([^<>]+)(</[^<>]+>)?$")


@dataclass(frozen=True)
class TransferOptions:
    """Limits and integrity requirements for one streaming transfer.

    ``max_bytes`` bounds the individual object while ``quota_bytes`` represents
    caller/provider capacity remaining before the operation starts. The smaller
    non-``None`` value is enforced. ``fetch()`` convenience methods still load
    the complete object in memory; these options apply to streaming methods.
    """

    max_bytes: int | None = DEFAULT_TRANSFER_MAX_BYTES
    quota_bytes: int | None = None
    timeout_seconds: float | None = DEFAULT_TRANSFER_TIMEOUT_SECONDS
    chunk_size: int = DEFAULT_TRANSFER_CHUNK_BYTES
    expected_sha256: str | None = None
    cancellation_event: asyncio.Event | None = field(default=None, repr=False, compare=False)
    diagnostic_hook: TransferDiagnosticHook | None = field(default=None, repr=False, compare=False)
    create_exclusive: bool = False
    object_metadata: Mapping[str, str] = field(default_factory=dict)
    # Trusted managed-write escape. Providers must restrict this to a validated
    # .agora/revisions/ destination; it never grants read access.
    allow_reserved: bool = False

    def __post_init__(self) -> None:
        for name in ("max_bytes", "quota_bytes"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ValueError(f"{name} must be a non-negative integer or None.")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive or None.")
        if isinstance(self.chunk_size, bool) or not isinstance(self.chunk_size, int) or self.chunk_size < 1:
            raise ValueError("chunk_size must be at least 1.")
        if self.expected_sha256 is not None:
            checksum = self.expected_sha256.lower()
            if len(checksum) != 64 or any(char not in "0123456789abcdef" for char in checksum):
                raise ValueError("expected_sha256 must be a 64-character hexadecimal digest.")
            object.__setattr__(self, "expected_sha256", checksum)
        metadata = dict(self.object_metadata)
        for key, value in metadata.items():
            if not isinstance(key, str) or not key or not isinstance(value, str):
                raise ValueError("object_metadata keys and values must be non-empty strings.")
            if "\r" in key or "\n" in key or "\r" in value or "\n" in value:
                raise ValueError("object_metadata must not contain line breaks.")
            if any(
                sensitive in key.lower()
                for sensitive in ("authorization", "credential", "password", "secret", "token", "sas")
            ):
                raise ValueError("object_metadata keys must not describe credential-bearing values.")
            if "://" in value:
                parsed = urlsplit(value)
                has_userinfo = parsed.password is not None or (
                    parsed.username is not None and parsed.scheme.lower() != "abfss"
                )
                if has_userinfo or parsed.query or parsed.fragment:
                    raise ValueError(
                        "object_metadata URI values must not contain user information, query parameters, or fragments."
                    )
        object.__setattr__(self, "object_metadata", MappingProxyType(metadata))

    @property
    def effective_max_bytes(self) -> int | None:
        """Return the tightest configured object/quota bound."""
        bounds = [value for value in (self.max_bytes, self.quota_bytes) if value is not None]
        return min(bounds) if bounds else None


@dataclass(frozen=True)
class TransferDiagnostic:
    """Credential-safe event delivered to an operator-provided audit hook."""

    operation: str
    state: str
    context: RequestContext
    resource: str | None = None
    bytes_transferred: int = 0
    checksum_sha256: str | None = None
    error_type: str | None = None


@dataclass(frozen=True)
class TransferResult:
    """Completed bounded transfer details, including the original caller context."""

    bytes_transferred: int
    checksum_sha256: str
    context: RequestContext
    resource: str | None = None
    elapsed_seconds: float = 0.0
    created: bool | None = None
    object_metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "object_metadata", MappingProxyType(dict(self.object_metadata)))


def safe_transfer_resource(value: str | os.PathLike[str] | None) -> str | None:
    """Return a credential-free resource identifier suitable for logs and events."""
    if value is None:
        return None
    text = os.fspath(value)
    return sanitize_uri_for_display(text) if "://" in text else text


def safe_artifact_reference(value: str) -> str:
    """Sanitize a URI nested inside a legacy ``<type>value</type>`` reference."""
    match = _TAGGED_REFERENCE_RE.fullmatch(value.strip())
    if match is None:
        return sanitize_uri_for_display(value) if "://" in value else value
    if "://" not in match.group(2):
        return value
    closing = match.group(3) or ""
    return f"{match.group(1)}{sanitize_uri_for_display(match.group(2))}{closing}"


async def emit_transfer_diagnostic(options: TransferOptions, diagnostic: TransferDiagnostic) -> None:
    """Invoke an optional diagnostics hook without changing transfer semantics."""
    hook = options.diagnostic_hook
    if hook is None:
        return
    try:
        result = hook(diagnostic)
        if inspect.isawaitable(result):
            await result
    except asyncio.CancelledError:
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise
        LOGGER.warning(
            "Transfer diagnostic hook cancelled itself for %s/%s",
            diagnostic.operation,
            diagnostic.state,
        )
    except Exception:
        LOGGER.warning(
            "Transfer diagnostic hook failed for %s/%s",
            diagnostic.operation,
            diagnostic.state,
            exc_info=True,
        )


def check_transfer_cancelled(options: TransferOptions, *, operation: str, resource: str | None = None) -> None:
    """Raise the stable cancellation error when cooperative cancellation was requested."""
    if options.cancellation_event is not None and options.cancellation_event.is_set():
        raise TransferCancelledError(
            "Transfer was cancelled.",
            resource_id=safe_transfer_resource(resource),
            operation=operation,
        )


def check_transfer_size(
    size: int,
    options: TransferOptions,
    *,
    operation: str,
    resource: str | None = None,
) -> None:
    """Reject a known or observed size above the configured object/quota bound."""
    limit = options.effective_max_bytes
    if limit is not None and size > limit:
        raise TransferLimitError(
            f"Transfer exceeds the configured {limit}-byte limit.",
            resource_id=safe_transfer_resource(resource),
            operation=operation,
        )


async def await_transfer(
    awaitable: Awaitable[object],
    options: TransferOptions,
    *,
    operation: str,
    resource: str | None = None,
) -> object:
    """Await an SDK operation with timeout and cooperative cancellation."""

    async def run() -> object:
        check_transfer_cancelled(options, operation=operation, resource=resource)
        task = asyncio.ensure_future(awaitable)
        cancel_task: asyncio.Task[bool] | None = None
        try:
            if options.cancellation_event is None:
                return await task
            cancel_task = asyncio.create_task(options.cancellation_event.wait())
            done, _ = await asyncio.wait((task, cancel_task), return_when=asyncio.FIRST_COMPLETED)
            if cancel_task in done:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise TransferCancelledError(
                    "Transfer was cancelled.",
                    resource_id=safe_transfer_resource(resource),
                    operation=operation,
                )
            return await task
        finally:
            if cancel_task is not None:
                cancel_task.cancel()

    try:
        if options.timeout_seconds is None:
            return await run()
        async with asyncio.timeout(options.timeout_seconds):
            return await run()
    except TimeoutError as exc:
        raise TransferTimeoutError(
            f"Transfer exceeded the configured {options.timeout_seconds:g}-second timeout.",
            resource_id=safe_transfer_resource(resource),
            operation=operation,
        ) from exc


async def stream_chunks_to_file(
    chunks: AsyncIterable[bytes],
    destination: str | os.PathLike[str],
    *,
    options: TransferOptions,
    context: RequestContext,
    operation: str = "download",
    resource: str | None = None,
) -> TransferResult:
    """Stream chunks into an atomically published file and remove partials on failure.

    Backpressure is provided by requesting the next chunk only after the current
    chunk has been written. Peak Workbench-owned payload memory is therefore
    bounded by one provider chunk plus ``options.chunk_size``.
    """
    destination_path = Path(os.path.abspath(os.fspath(destination)))
    temporary_name = f".{destination_path.name}.{secrets.token_hex(8)}.part"
    temporary_path = destination_path.with_name(temporary_name)
    parent_fd: int | None = None
    display_resource = safe_transfer_resource(resource)
    started = time.monotonic()
    bytes_transferred = 0
    digest = hashlib.sha256()
    await emit_transfer_diagnostic(
        options,
        TransferDiagnostic(operation, "started", context, display_resource),
    )

    async def copy() -> None:
        nonlocal bytes_transferred
        if parent_fd is None:
            output_file = temporary_path.open("xb", buffering=0)
        else:
            output_descriptor = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=parent_fd,
            )
            output_file = os.fdopen(output_descriptor, "wb", buffering=0, closefd=True)
        with output_file as output:
            async for provider_chunk in chunks:
                check_transfer_cancelled(options, operation=operation, resource=resource)
                view = memoryview(provider_chunk)
                for offset in range(0, len(view), options.chunk_size):
                    chunk = view[offset : offset + options.chunk_size]
                    check_transfer_size(
                        bytes_transferred + len(chunk),
                        options,
                        operation=operation,
                        resource=resource,
                    )
                    output.write(chunk)
                    digest.update(chunk)
                    bytes_transferred += len(chunk)
            output.flush()
            os.fsync(output.fileno())

    def cleanup_temporary() -> None:
        try:
            if parent_fd is None:
                temporary_path.unlink(missing_ok=True)
            else:
                os.unlink(temporary_name, dir_fd=parent_fd)
        except FileNotFoundError:
            return

    try:
        if os.name == "posix":
            parent_fd = os.open(
                os.path.sep,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                for part in destination_path.parts[1:-1]:
                    try:
                        os.mkdir(part, mode=0o750, dir_fd=parent_fd)
                    except FileExistsError:
                        LOGGER.debug("Transfer destination directory already exists: %s", part)
                    next_fd = os.open(
                        part,
                        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=parent_fd,
                    )
                    os.close(parent_fd)
                    parent_fd = next_fd
            except BaseException:
                os.close(parent_fd)
                parent_fd = None
                raise
        else:
            destination_path.parent.mkdir(parents=True, exist_ok=True)
        if options.timeout_seconds is None:
            await copy()
        else:
            async with asyncio.timeout(options.timeout_seconds):
                await copy()
        check_transfer_cancelled(options, operation=operation, resource=resource)
        actual_checksum = digest.hexdigest()
        if options.expected_sha256 is not None and not secrets.compare_digest(actual_checksum, options.expected_sha256):
            raise TransferChecksumError(
                "Transfer checksum did not match the expected SHA-256 digest.",
                resource_id=display_resource,
                operation=operation,
            )
        if options.create_exclusive:
            if parent_fd is None:
                os.link(temporary_path, destination_path)
                temporary_path.unlink()
            else:
                os.link(
                    temporary_name,
                    destination_path.name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                os.unlink(temporary_name, dir_fd=parent_fd)
        else:
            if parent_fd is None:
                os.replace(temporary_path, destination_path)
            else:
                os.replace(
                    temporary_name,
                    destination_path.name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
    except asyncio.CancelledError:
        cleanup_temporary()
        await emit_transfer_diagnostic(
            options,
            TransferDiagnostic(operation, "cancelled", context, display_resource, bytes_transferred),
        )
        raise
    except TimeoutError as exc:
        cleanup_temporary()
        error = TransferTimeoutError(
            f"Transfer exceeded the configured {options.timeout_seconds:g}-second timeout.",
            resource_id=display_resource,
            operation=operation,
        )
        await emit_transfer_diagnostic(
            options,
            TransferDiagnostic(
                operation, "failed", context, display_resource, bytes_transferred, error_type=type(error).__name__
            ),
        )
        raise error from exc
    except BaseException as exc:
        cleanup_temporary()
        await emit_transfer_diagnostic(
            options,
            TransferDiagnostic(
                operation, "failed", context, display_resource, bytes_transferred, error_type=type(exc).__name__
            ),
        )
        raise
    finally:
        if parent_fd is not None:
            os.close(parent_fd)

    result = TransferResult(
        bytes_transferred=bytes_transferred,
        checksum_sha256=digest.hexdigest(),
        context=context,
        resource=display_resource,
        elapsed_seconds=time.monotonic() - started,
        created=True if options.create_exclusive else None,
        object_metadata=options.object_metadata,
    )
    await emit_transfer_diagnostic(
        options,
        TransferDiagnostic(
            operation,
            "completed",
            context,
            display_resource,
            result.bytes_transferred,
            result.checksum_sha256,
        ),
    )
    return result


async def hash_file(
    source: BinaryIO,
    *,
    options: TransferOptions,
    context: RequestContext,
    operation: str,
    resource: str | None = None,
) -> TransferResult:
    """Hash a file-like source with the same limits used for its upload."""
    started = time.monotonic()
    digest = hashlib.sha256()
    total = 0

    async def run() -> None:
        nonlocal total
        while True:
            check_transfer_cancelled(options, operation=operation, resource=resource)
            chunk = source.read(options.chunk_size)
            if not chunk:
                break
            total += len(chunk)
            check_transfer_size(total, options, operation=operation, resource=resource)
            digest.update(chunk)
            await asyncio.sleep(0)

    try:
        if options.timeout_seconds is None:
            await run()
        else:
            async with asyncio.timeout(options.timeout_seconds):
                await run()
    except TimeoutError as exc:
        raise TransferTimeoutError(
            f"Transfer exceeded the configured {options.timeout_seconds:g}-second timeout.",
            resource_id=safe_transfer_resource(resource),
            operation=operation,
        ) from exc
    actual_checksum = digest.hexdigest()
    if options.expected_sha256 is not None and not secrets.compare_digest(actual_checksum, options.expected_sha256):
        raise TransferChecksumError(
            "Transfer checksum did not match the expected SHA-256 digest.",
            resource_id=safe_transfer_resource(resource),
            operation=operation,
        )
    return TransferResult(total, actual_checksum, context, safe_transfer_resource(resource), time.monotonic() - started)


__all__ = [
    "DEFAULT_TRANSFER_CHUNK_BYTES",
    "DEFAULT_TRANSFER_MAX_BYTES",
    "DEFAULT_TRANSFER_TIMEOUT_SECONDS",
    "TransferDiagnostic",
    "TransferDiagnosticHook",
    "TransferOptions",
    "TransferResult",
    "await_transfer",
    "check_transfer_cancelled",
    "check_transfer_size",
    "emit_transfer_diagnostic",
    "hash_file",
    "safe_transfer_resource",
    "safe_artifact_reference",
    "stream_chunks_to_file",
]
