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
import logging
import os
from typing import Any
from urllib.parse import urlparse

import dill

from . import agent_guidance

LOGGER = logging.getLogger(__name__)

# Maximum serialized object size (256 MB).  Objects exceeding this limit
# are rejected to prevent accidental memory exhaustion.
MAX_TRANSFER_SIZE_BYTES = 256 * 1024 * 1024

# Loopback hostnames that are always permitted for local development / testing.
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _validate_target_url(url: str) -> None:
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
      All other plain-HTTP destinations are rejected to prevent bearer-token
      exposure over unencrypted connections.
    * When the environment variable ``OBJECT_TRANSFER_ALLOWED_HOSTS`` is set,
      the URL's hostname must match one of the space- or comma-separated
      patterns listed there.  Each pattern may use ``*`` as a leading wildcard
      (e.g. ``*.azurecontainerapps.io``).  Loopback hosts always bypass this
      check.

    Args:
        url: The target URL to validate.

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
        if not (trusted_patterns and _host_matches_any(host, trusted_patterns)):
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
