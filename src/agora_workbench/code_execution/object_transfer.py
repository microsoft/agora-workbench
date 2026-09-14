"""
Server-to-server object transfer utilities for MCP code execution servers.

Provides URL validation, serialization helpers, and constants used by
:class:`~.data_access.publishers.ServerPublisher` (which handles the actual
HTTP transfer) and the ``/object-transfer/receive`` endpoint in ``server.py``.

Typical flow (agent-triggered):
    1. Agent calls ``{source}_send(data_ref="var", to="gis")``
    2. Source server serializes the named variable from the kernel namespace
    3. ServerPublisher POSTs the serialized payload to target's ``/object-transfer/receive``
    4. Target server deserializes and injects the object into its kernel namespace
    5. Agent can now reference the variable by name on the target server
"""

import base64
import binascii

import json
import logging
import math
import os
from collections.abc import AsyncIterable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import dill

from agora_workbench.data_lake import RequestContext
from agora_workbench.data_lake.transfer import (
    TransferOptions,
    TransferResult,
    _run_blocking_io,
    check_transfer_size,
    stream_chunks_to_file,
)

from . import agent_guidance

LOGGER = logging.getLogger(__name__)

# Maximum serialized object size (256 MB).  Objects exceeding this limit
# are rejected to prevent accidental memory exhaustion.
MAX_TRANSFER_SIZE_BYTES = 256 * 1024 * 1024
STREAMING_TRANSFER_VERSION = "2"
STREAMING_TRANSFER_VERSION_HEADER = "X-Agora-Object-Transfer-Version"
STREAMING_TRANSFER_INFO_HEADER = "X-Agora-Object-Transfer-Info"
_STREAMING_DATA_MARKER = b',"data":"'
_MAX_STREAMING_ENVELOPE_BYTES = 64 * 1024

# Loopback hostnames that are always permitted for local development / testing.
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def encode_streaming_transfer_info(
    *,
    variable_name: str,
    session_id: str,
    metadata: dict[str, Any],
    size_bytes: int,
    checksum_sha256: str,
) -> str:
    """Encode bounded metadata for the versioned streaming receive path."""
    payload = json.dumps(
        {
            "variable_name": variable_name,
            "session_id": session_id,
            "metadata": metadata,
            "size_bytes": size_bytes,
            "checksum_sha256": checksum_sha256,
        },
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode()


def decode_streaming_transfer_info(value: str) -> dict[str, Any]:
    """Decode and validate the versioned streaming transfer header."""
    if not value or len(value) > _MAX_STREAMING_ENVELOPE_BYTES:
        raise ValueError("Invalid streaming transfer metadata.")
    try:
        decoded = base64.b64decode(value, altchars=b"-_", validate=True)
        payload = json.loads(decoded)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid streaming transfer metadata.") from exc
    if not isinstance(payload, dict):
        raise ValueError("Invalid streaming transfer metadata.")
    return payload


async def receive_streaming_transfer(
    chunks: AsyncIterable[bytes],
    destination: Path,
    *,
    expected_size: int,
    expected_sha256: str,
    options: TransferOptions,
    context: RequestContext,
) -> TransferResult:
    """Incrementally decode a versioned JSON/base64 request into a bounded file."""
    effective_max_bytes = options.effective_max_bytes
    max_encoded_bytes = math.ceil(effective_max_bytes * 4 / 3) + 4 if effective_max_bytes is not None else None

    async def decoded_chunks():
        prefix = bytearray()
        remainder = b""
        reading_data = False
        data_complete = False
        total_encoded = 0

        async for chunk in chunks:
            total_encoded += len(chunk)
            if max_encoded_bytes is not None:
                check_transfer_size(
                    max(0, total_encoded - _MAX_STREAMING_ENVELOPE_BYTES),
                    TransferOptions(max_bytes=max_encoded_bytes),
                    operation="receive",
                    resource="peer object transfer",
                )
            if data_complete:
                continue
            data = chunk
            if not reading_data:
                prefix.extend(data)
                marker_index = prefix.find(_STREAMING_DATA_MARKER)
                if marker_index < 0:
                    if len(prefix) > _MAX_STREAMING_ENVELOPE_BYTES:
                        raise ValueError("Invalid streaming object transfer envelope.")
                    continue
                data = bytes(prefix[marker_index + len(_STREAMING_DATA_MARKER) :])
                prefix.clear()
                reading_data = True

            closing_quote = data.find(b'"')
            encoded = data if closing_quote < 0 else data[:closing_quote]
            if closing_quote >= 0:
                data_complete = True
            encoded = remainder + encoded
            complete_length = len(encoded) if data_complete else len(encoded) - (len(encoded) % 4)
            if complete_length:
                try:
                    decoded = base64.b64decode(encoded[:complete_length], validate=True)
                except binascii.Error as exc:
                    raise ValueError("Invalid base64 data.") from exc
                if decoded:
                    yield decoded
            remainder = encoded[complete_length:]

        if not reading_data or not data_complete or remainder:
            raise ValueError("Invalid streaming object transfer envelope.")

    result = await stream_chunks_to_file(
        decoded_chunks(),
        destination,
        options=options,
        context=context,
        operation="receive",
        resource="peer object transfer",
    )
    if result.bytes_transferred != expected_size:
        await _run_blocking_io(
            lambda: destination.unlink(missing_ok=True),
            operation="receive",
            resource="peer object transfer",
        )
        raise ValueError("Decoded payload size did not match the declared size.")
    if result.checksum_sha256 != expected_sha256:
        await _run_blocking_io(
            lambda: destination.unlink(missing_ok=True),
            operation="receive",
            resource="peer object transfer",
        )
        raise ValueError("Decoded payload checksum did not match the declared checksum.")
    return result


def _validate_target_url(url: str, trust_http: bool = False) -> None:
    """Validate a target URL before sending credentials.

    Enforces the following rules to prevent SSRF and Bearer-token leakage:

    * The URL must use the ``https`` scheme, *unless* the host is one of:
        - a loopback address (``localhost``, ``127.0.0.1``, ``::1``), which is
          always permitted over plain ``http`` for local development / tests;
        - a host matching a pattern in the ``OBJECT_TRANSFER_TRUSTED_HTTP_HOSTS``
          environment variable, which lets operators explicitly opt-in trusted
          internal service names (e.g. docker-compose service names on a
          shared docker network).  Patterns follow the same syntax as
          ``OBJECT_TRANSFER_ALLOWED_HOSTS`` (space- or comma-separated, optional
          leading ``*.`` wildcard).
        - the caller passed ``trust_http=True``, which signals the URL came
          from an operator-configured source that already encodes the scheme
          choice (e.g. a peer in ``AGORA_PEER_REGISTRY`` / ``peer_registry``).
          In that case the operator typed the ``http://`` URL themselves, so
          re-listing the host in ``OBJECT_TRANSFER_TRUSTED_HTTP_HOSTS`` would
          be redundant.
      All other plain-HTTP destinations are rejected to prevent bearer-token
      exposure over unencrypted connections.
    * When the environment variable ``OBJECT_TRANSFER_ALLOWED_HOSTS`` is set,
      the URL's hostname must match one of the space- or comma-separated
      patterns listed there.  Each pattern may use ``*`` as a leading wildcard
      (e.g. ``*.azurecontainerapps.io``).  Loopback hosts always bypass this
      check.

    Args:
        url: The target URL to validate.
        trust_http: When ``True``, allow plain HTTP to a non-loopback host
            without requiring it in ``OBJECT_TRANSFER_TRUSTED_HTTP_HOSTS``.
            Set by callers whose URL originates from an operator-curated
            registry (the operator already chose the scheme). The
            ``OBJECT_TRANSFER_ALLOWED_HOSTS`` SSRF check still applies.

    Raises:
        ValueError: If the URL fails any validation rule.
    """
    parsed = urlparse(url)
    host = parsed.hostname or ""

    if not host:
        raise ValueError("Object transfer target URL must include a hostname.")

    is_loopback = host in _LOOPBACK_HOSTS

    # Only allow HTTP(S); plain HTTP is restricted to loopback addresses and
    # explicitly-opted-in trusted hosts.
    if parsed.scheme not in ("https", "http"):
        raise ValueError(f"Object transfer target URL must use HTTP or HTTPS (got '{parsed.scheme}').")
    if parsed.scheme == "http" and not is_loopback:
        trusted_patterns = _parse_host_patterns(os.environ.get("OBJECT_TRANSFER_TRUSTED_HTTP_HOSTS", ""))
        host_is_trusted = trust_http or (trusted_patterns and _host_matches_any(host, trusted_patterns))
        if not host_is_trusted:
            raise ValueError(
                agent_guidance.operator_gate(
                    f"Plain HTTP to '{host}' is only permitted for loopback addresses "
                    "(localhost, 127.0.0.1, ::1) or explicitly trusted hosts.",
                    tell_user=("use an HTTPS URL, or ask the operator to add this host to the trusted list."),
                    env_var="OBJECT_TRANSFER_TRUSTED_HTTP_HOSTS",
                )
            )
        LOGGER.warning(
            "Allowing plain-HTTP object transfer to trusted host '%s'. "
            "Bearer tokens will traverse an unencrypted connection; ensure "
            "this is acceptable for the deployment (e.g. a private docker network).",
            host,
        )

    # Honor an optional hostname allow-list from the environment.
    # Loopback hosts bypass this check; trusted HTTP hosts do *not* so that the
    # allow-list remains an effective SSRF control for internal service names.
    allowed_patterns = _parse_host_patterns(os.environ.get("OBJECT_TRANSFER_ALLOWED_HOSTS", ""))
    if allowed_patterns and not is_loopback:
        if not _host_matches_any(host, allowed_patterns):
            raise ValueError(
                agent_guidance.operator_gate(
                    f"Object transfer target host '{host}' is not in the allowed-host list.",
                    tell_user="ask the operator to add this host if the transfer is expected.",
                    env_var="OBJECT_TRANSFER_ALLOWED_HOSTS",
                )
            )


def _parse_host_patterns(raw: str) -> list[str]:
    """Split a comma- or space-separated env-var value into host patterns."""
    return [p.strip() for p in raw.replace(",", " ").split() if p.strip()]


def _host_matches_any(host: str, patterns: list[str]) -> bool:
    """Return True if *host* matches any pattern in *patterns*.

    Supports a single leading ``*`` wildcard (e.g. ``*.azurecontainerapps.io``).
    The wildcard matches only proper subdomains; the base domain itself is not matched.

    Patterns are normalized to lowercase and stripped of a trailing dot before
    matching, so ``*.AzureContainerApps.IO.`` behaves identically to
    ``*.azurecontainerapps.io``.
    """
    for raw_pattern in patterns:
        pattern = raw_pattern.lower().rstrip(".")
        if pattern.startswith("*."):
            suffix = pattern[1:]  # e.g. ".azurecontainerapps.io"
            if host.endswith(suffix):
                return True
        else:
            if host == pattern:
                return True
    return False


class ObjectSerializer:
    """Serialize Python objects for cross-server transfer.

    Uses ``dill`` which supports a broader range of Python types than the
    standard ``pickle`` module (lambdas, closures, nested classes, etc.).

    **Security note**: ``serialize`` is a general-purpose helper and is not
    restricted to the trusted kernel process. The critical security invariant
    is that deserialization of network-received payloads must **never** be
    performed in the server process — it must always happen inside the
    sandboxed Jupyter kernel via ``execute_code_for_session``. The receive
    endpoint in ``server.py`` follows this requirement by writing the raw bytes
    to a temp file and loading them with ``dill.load`` inside the kernel.
    """

    @staticmethod
    def serialize(obj: Any) -> bytes:
        """Serialize a Python object to bytes.

        Args:
            obj: Any picklable/dillable Python object.

        Returns:
            Serialized bytes.

        Raises:
            TypeError: If the object cannot be serialized.
            ValueError: If the serialized payload exceeds the size limit.
        """
        try:
            data = dill.dumps(obj, protocol=dill.HIGHEST_PROTOCOL)
        except Exception as exc:
            raise TypeError(f"Object cannot be serialized: {exc}") from exc

        if len(data) > MAX_TRANSFER_SIZE_BYTES:
            raise ValueError(
                agent_guidance.operator_gate(
                    f"Serialized object size ({len(data):,} bytes) exceeds limit ({MAX_TRANSFER_SIZE_BYTES:,} bytes).",
                    tell_user="reduce the object size or split the transfer into smaller pieces.",
                )
            )
        return data

    @staticmethod
    def to_base64(data: bytes) -> str:
        """Encode raw bytes as a base64 string."""
        return base64.b64encode(data).decode("ascii")

    @staticmethod
    def from_base64(encoded: str) -> bytes:
        """Decode a base64 string back to raw bytes."""
        return base64.b64decode(encoded)
