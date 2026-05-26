"""
Server-to-server object transfer for MCP code execution servers.

Enables direct transfer of Python objects between MCP servers without
routing through the agent context. Objects are serialized with dill
in the source kernel, transmitted via HTTP, and deserialized into the
target server's kernel namespace.

Typical flow (agent-triggered):
    1. Agent calls ``{source}_push_object(target_url, variable_name, target_variable_name)``
    2. Source server serializes the named variable from the kernel namespace
    3. Source server POSTs the serialized payload to target's ``/object-transfer/receive``
    4. Target server deserializes and injects the object into its kernel namespace
    5. Agent can now reference the variable by name on the target server
"""

import base64
import logging
import os
from typing import Any, Optional
from urllib.parse import urlparse

import dill
import httpx

LOGGER = logging.getLogger(__name__)

# Maximum serialized object size (256 MB).  Objects exceeding this limit
# are rejected to prevent accidental memory exhaustion.
MAX_TRANSFER_SIZE_BYTES = 256 * 1024 * 1024

# Loopback hostnames that are always permitted for local development / testing.
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _validate_target_url(url: str) -> None:
    """Validate a target URL before sending credentials.

    Enforces the following rules to prevent SSRF and Bearer-token leakage:

    * The URL must use the ``https`` scheme, *unless* the host is a loopback
      address (``localhost``, ``127.0.0.1``, ``::1``), which is permitted over
      plain ``http`` for local development and tests.  All other plain-HTTP
      destinations are rejected to prevent bearer-token exposure over
      unencrypted connections.
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

    # Only allow HTTP(S); plain HTTP is restricted to loopback addresses only.
    if parsed.scheme not in ("https", "http"):
        raise ValueError(f"Object transfer target URL must use HTTP or HTTPS (got '{parsed.scheme}').")
    if parsed.scheme == "http" and not is_loopback:
        raise ValueError(
            "Plain HTTP is only permitted for loopback addresses "
            "(localhost, 127.0.0.1, ::1). "
            "Use an HTTPS URL for all other inter-service object transfers."
        )

    # Honor an optional hostname allow-list from the environment.
    # Loopback hosts bypass this check; trusted HTTP hosts do *not* so that the
    # allow-list remains an effective SSRF control for internal service names.
    allowed_hosts_env = os.environ.get("OBJECT_TRANSFER_ALLOWED_HOSTS", "").strip()
    if allowed_hosts_env and not is_loopback:
        patterns = [p.strip() for p in allowed_hosts_env.replace(",", " ").split() if p.strip()]
        if patterns and not _host_matches_any(host, patterns):
            raise ValueError(
                f"Object transfer target host '{host}' is not in the allowed-host list. "
                f"Update OBJECT_TRANSFER_ALLOWED_HOSTS to permit this host."
            )


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
                f"Serialized object size ({len(data):,} bytes) exceeds limit ({MAX_TRANSFER_SIZE_BYTES:,} bytes)."
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


class ObjectTransferClient:
    """HTTP client for pushing serialized objects to a remote MCP server.

    The client authenticates to the target server using the caller's
    bearer token (forwarded from the current session).
    """

    def __init__(self, user_token: str, timeout: float = 60.0):
        self._user_token = user_token
        self._timeout = timeout

    async def push(
        self,
        target_url: str,
        variable_name: str,
        serialized_data: bytes,
        metadata: Optional[dict] = None,
        target_session_id: Optional[str] = None,
    ) -> dict:
        """Push a serialized object to a remote server's receive endpoint.

        Args:
            target_url: Base URL of the target server (e.g. ``https://host:8001``).
                        ``/object-transfer/receive`` is appended automatically.
                        Must use ``https://`` unless the host is a loopback address.
            variable_name: Variable name to assign on the target server.
            serialized_data: Object bytes produced by :class:`ObjectSerializer`.
            metadata: Optional metadata dict to store alongside the object.
            target_session_id: Optional session ID on the target server.  If
                provided, the object is stored in that specific session.

        Returns:
            Response payload from the target server.

        Raises:
            ValueError: If ``target_url`` fails HTTPS or hostname allow-list
                validation (see :func:`_validate_target_url`).
            httpx.HTTPStatusError: On non-2xx responses.
            httpx.RequestError: On connection / timeout errors.
        """
        _validate_target_url(target_url)
        # Strip common MCP path suffixes so the agent can pass the MCP
        # endpoint URL directly (e.g. http://gis-server:8000/mcp).
        import re as _re

        base = _re.sub(r"/mcp/?$", "", target_url.rstrip("/"))
        receive_url = f"{base}/object-transfer/receive"
        payload: dict = {
            "variable_name": variable_name,
            "data": ObjectSerializer.to_base64(serialized_data),
            "metadata": metadata or {},
        }
        if target_session_id:
            payload["session_id"] = target_session_id

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=self._timeout, write=self._timeout, pool=10.0),
        ) as client:
            response = await client.post(
                receive_url,
                json=payload,
                headers={"Authorization": f"Bearer {self._user_token}"},
            )
            response.raise_for_status()
            return response.json()
