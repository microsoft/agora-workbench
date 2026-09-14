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
import re
from collections.abc import AsyncIterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import dill

from agora_workbench.data_lake import RequestContext, TransferLimitError
from agora_workbench.data_lake.transfer import (
    TransferOptions,
    TransferResult,
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
_LEGACY_DATA_MARKER_RE = re.compile(rb',\s*"data"\s*:\s*"')
_MAX_STREAMING_ENVELOPE_BYTES = 64 * 1024
MAX_TRANSFER_BODY_BYTES = math.ceil(MAX_TRANSFER_SIZE_BYTES * 4 / 3) + 4 + (2 * _MAX_STREAMING_ENVELOPE_BYTES)
_MAX_CORRELATION_ID_LENGTH = 128
_CORRELATION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*\Z")

# Loopback hostnames that are always permitted for local development / testing.
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def parse_object_transfer_version(value: str | None) -> bool:
    """Return whether the exact supported streaming version was requested."""
    if value is None:
        return False
    if value == STREAMING_TRANSFER_VERSION:
        return True
    raise ValueError("Unsupported object transfer version.")


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


def parse_transfer_correlation_metadata(metadata: Any) -> tuple[str | None, str | None]:
    """Validate optional opaque identifiers used in peer transfer diagnostics."""
    if not isinstance(metadata, dict):
        raise ValueError("Invalid object transfer correlation metadata.")

    identifiers: list[str | None] = []
    for field in ("source_server", "transfer_id"):
        value = metadata.get(field)
        if value in (None, ""):
            identifiers.append(None)
            continue
        if (
            not isinstance(value, str)
            or len(value) > _MAX_CORRELATION_ID_LENGTH
            or _CORRELATION_ID_RE.fullmatch(value) is None
        ):
            raise ValueError("Invalid object transfer correlation metadata.")
        identifiers.append(value)
    return identifiers[0], identifiers[1]


def _validate_streaming_envelope_suffix(suffix: bytes) -> None:
    """Validate the bounded JSON tail following the streamed base64 string."""

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Invalid streaming object transfer envelope.")
            result[key] = value
        return result

    try:
        envelope = json.loads(b'{"data":""' + suffix, object_pairs_hook=reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid streaming object transfer envelope.") from exc
    if not isinstance(envelope, dict):
        raise ValueError("Invalid streaming object transfer envelope.")
    expected_keys = {"data", "metadata"}
    if "session_id" in envelope:
        expected_keys.add("session_id")
    if (
        set(envelope) != expected_keys
        or not isinstance(envelope["metadata"], dict)
        or ("session_id" in envelope and not isinstance(envelope["session_id"], str))
    ):
        raise ValueError("Invalid streaming object transfer envelope.")


@dataclass(frozen=True)
class LegacyTransferEnvelope:
    """Validated legacy v1 metadata accompanying a streamed payload."""

    variable_name: str
    session_id: str
    metadata: dict[str, Any]
    result: TransferResult


async def receive_legacy_streaming_transfer(
    chunks: AsyncIterable[bytes],
    destination: Path,
    *,
    options: TransferOptions,
    context: RequestContext,
    _destination_parent_fd: int | None = None,
) -> LegacyTransferEnvelope:
    """Incrementally decode a legacy v1 JSON/base64 envelope with bounded memory."""
    prefix = bytearray()
    suffix = bytearray()
    remainder = b""
    reading_data = False
    data_complete = False
    total_body_bytes = 0
    validated_envelope: dict[str, Any] | None = None

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        parsed: dict[str, Any] = {}
        for key, value in pairs:
            if key in parsed:
                raise ValueError("Invalid legacy object transfer envelope.")
            parsed[key] = value
        return parsed

    def validate_envelope() -> dict[str, Any]:
        try:
            envelope = json.loads(
                bytes(prefix) + b',"data":""' + bytes(suffix),
                object_pairs_hook=reject_duplicate_keys,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError("Invalid legacy object transfer envelope.") from exc
        expected_keys = {"variable_name", "data", "metadata"}
        if isinstance(envelope, dict) and "session_id" in envelope:
            expected_keys.add("session_id")
        if (
            not isinstance(envelope, dict)
            or set(envelope) != expected_keys
            or not isinstance(envelope.get("variable_name"), str)
            or not isinstance(envelope.get("metadata"), dict)
            or ("session_id" in envelope and not isinstance(envelope["session_id"], str))
        ):
            raise ValueError("Invalid legacy object transfer envelope.")
        return envelope

    async def decoded_chunks():
        nonlocal remainder, reading_data, data_complete, total_body_bytes, validated_envelope
        async for chunk in chunks:
            total_body_bytes += len(chunk)
            if total_body_bytes > MAX_TRANSFER_BODY_BYTES:
                raise TransferLimitError(
                    "Object transfer request body exceeds the maximum encoded size.",
                    resource_id="peer object transfer",
                    operation="receive",
                )
            if data_complete:
                suffix.extend(chunk)
                if len(suffix) > _MAX_STREAMING_ENVELOPE_BYTES:
                    raise ValueError("Invalid legacy object transfer envelope.")
                continue
            data = chunk
            if not reading_data:
                prefix.extend(data)
                marker = _LEGACY_DATA_MARKER_RE.search(prefix)
                if marker is None:
                    if len(prefix) > _MAX_STREAMING_ENVELOPE_BYTES:
                        raise ValueError("Invalid legacy object transfer envelope.")
                    continue
                data = bytes(prefix[marker.end() :])
                del prefix[marker.start() :]
                reading_data = True

            closing_quote = data.find(b'"')
            encoded = data if closing_quote < 0 else data[:closing_quote]
            if closing_quote >= 0:
                data_complete = True
                suffix.extend(data[closing_quote + 1 :])
                if len(suffix) > _MAX_STREAMING_ENVELOPE_BYTES:
                    raise ValueError("Invalid legacy object transfer envelope.")
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
            raise ValueError("Invalid legacy object transfer envelope.")
        validated_envelope = validate_envelope()

    result = await stream_chunks_to_file(
        decoded_chunks(),
        destination,
        options=options,
        context=context,
        operation="receive",
        resource="peer object transfer",
        _destination_parent_fd=_destination_parent_fd,
    )
    assert validated_envelope is not None
    return LegacyTransferEnvelope(
        variable_name=validated_envelope["variable_name"],
        session_id=validated_envelope.get("session_id", ""),
        metadata=validated_envelope["metadata"],
        result=result,
    )


async def receive_streaming_transfer(
    chunks: AsyncIterable[bytes],
    destination: Path,
    *,
    expected_size: int,
    expected_sha256: str,
    options: TransferOptions,
    context: RequestContext,
    _destination_parent_fd: int | None = None,
) -> TransferResult:
    """Incrementally decode a versioned JSON/base64 request into a bounded file."""
    transfer_options = replace(options, expected_sha256=expected_sha256)
    if options.expected_sha256 not in (None, transfer_options.expected_sha256):
        raise ValueError("Transfer options checksum does not match the declared checksum.")
    effective_max_bytes = options.effective_max_bytes
    max_encoded_bytes = math.ceil(effective_max_bytes * 4 / 3) + 4 if effective_max_bytes is not None else None

    async def decoded_chunks():
        prefix = bytearray()
        remainder = b""
        suffix = bytearray()
        reading_data = False
        data_complete = False
        total_encoded = 0
        total_decoded = 0

        async for chunk in chunks:
            total_encoded += len(chunk)
            encoded_payload_bytes = max(0, total_encoded - _MAX_STREAMING_ENVELOPE_BYTES)
            if max_encoded_bytes is not None and encoded_payload_bytes > max_encoded_bytes:
                raise TransferLimitError(
                    f"Transfer exceeds the configured {max_encoded_bytes}-byte encoded limit.",
                    resource_id="peer object transfer",
                    operation="receive",
                )
            if data_complete:
                suffix.extend(chunk)
                if len(suffix) > _MAX_STREAMING_ENVELOPE_BYTES:
                    raise ValueError("Invalid streaming object transfer envelope.")
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
                suffix.extend(data[closing_quote + 1 :])
                if len(suffix) > _MAX_STREAMING_ENVELOPE_BYTES:
                    raise ValueError("Invalid streaming object transfer envelope.")
            encoded = remainder + encoded
            complete_length = len(encoded) if data_complete else len(encoded) - (len(encoded) % 4)
            if complete_length:
                try:
                    decoded = base64.b64decode(encoded[:complete_length], validate=True)
                except binascii.Error as exc:
                    raise ValueError("Invalid base64 data.") from exc
                if decoded:
                    total_decoded += len(decoded)
                    if total_decoded > expected_size:
                        raise ValueError("Decoded payload exceeded the declared size.")
                    yield decoded
            remainder = encoded[complete_length:]

        if not reading_data or not data_complete or remainder:
            raise ValueError("Invalid streaming object transfer envelope.")
        _validate_streaming_envelope_suffix(bytes(suffix))
        if total_decoded != expected_size:
            raise ValueError("Decoded payload size did not match the declared size.")

    return await stream_chunks_to_file(
        decoded_chunks(),
        destination,
        options=transfer_options,
        context=context,
        operation="receive",
        resource="peer object transfer",
        _destination_parent_fd=_destination_parent_fd,
    )


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
    if "\\" in url or any(ord(char) <= 32 or ord(char) == 127 for char in url):
        raise ValueError("Object transfer target URL contains an invalid or ambiguous character.")
    parsed = urlparse(url)
    try:
        username = parsed.username
        password = parsed.password
        port = parsed.port
        host = (parsed.hostname or "").lower().rstrip(".")
    except ValueError as exc:
        raise ValueError("Object transfer target URL has an invalid authority.") from exc

    if not host:
        raise ValueError("Object transfer target URL must include a hostname.")
    if username is not None or password is not None or "@" in parsed.netloc:
        raise ValueError("Object transfer target URL must not include user information.")
    if "%" in host:
        raise ValueError("Object transfer target URL hostname must not contain percent-encoding.")
    if parsed.query or parsed.fragment:
        raise ValueError("Object transfer target URL must not include a query string or fragment.")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Object transfer target URL has an invalid port.")

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
