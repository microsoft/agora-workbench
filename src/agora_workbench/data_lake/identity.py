"""Public artifact identity and storage-location normalization helpers."""

from __future__ import annotations

import hashlib
import posixpath
import re
import uuid
from pathlib import PurePosixPath
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from .errors import InvalidRequestError

_LOGICAL_ID_NAMESPACE = uuid.UUID("37d400b9-1908-4a2d-a44b-b6a20e2492e8")
_ACCOUNT_RE = re.compile(r"^[a-z0-9]{3,24}$")
_CONTAINER_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])$")
_SYSTEM_CONTAINERS = frozenset({"$logs", "$root", "$web"})


def _invalid_identity(message: str) -> InvalidRequestError:
    return InvalidRequestError(message, operation="identity")


def normalize_logical_path(path: str) -> str:
    """Return a portable, source-relative POSIX path."""
    candidate = path.replace("\\", "/").lstrip("/")
    normalized = posixpath.normpath(candidate)
    if normalized in {"", "."}:
        raise _invalid_identity("Artifact path must identify an object.")
    if normalized == ".." or normalized.startswith("../"):
        raise _invalid_identity("Artifact path must stay within its source.")
    return str(PurePosixPath(normalized))


def sanitize_uri_for_display(uri: str) -> str:
    """Remove credentials, query parameters, and fragments from a URI."""
    parsed = urlsplit(uri)
    if parsed.scheme.lower() == "abfss":
        container = parsed.username or ""
        hostname = parsed.hostname or ""
        netloc = f"{container}@{hostname}" if container else hostname
        return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    hostname = parsed.hostname or ""
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is not None:
        hostname = f"{hostname}:{port}"
    return urlunsplit((parsed.scheme, hostname, parsed.path, "", ""))


def parse_azure_uri(uri: str) -> tuple[str, str, str]:
    """Parse a supported Azure URI into account, container, and decoded object path."""
    parsed = urlsplit(uri)
    scheme = parsed.scheme.lower()
    if scheme != "abfss" and (parsed.username is not None or parsed.password is not None):
        raise _invalid_identity("Azure storage URI must not contain user information.")
    if scheme != "abfss":
        try:
            if parsed.port is not None:
                raise _invalid_identity("Azure storage URI ports are not supported.")
        except ValueError as exc:
            raise _invalid_identity("Azure storage URI contains an invalid port.") from exc

    if scheme == "az":
        account = parsed.hostname or ""
        parts = parsed.path.lstrip("/").split("/", 1)
        container = parts[0] if parts else ""
        encoded_path = parts[1] if len(parts) > 1 else ""
    elif scheme in {"http", "https"}:
        host = (parsed.hostname or "").lower()
        suffix = next(
            (
                candidate
                for candidate in (".blob.core.windows.net", ".dfs.core.windows.net")
                if host.endswith(candidate)
            ),
            None,
        )
        if suffix is None:
            raise _invalid_identity("Unsupported Azure storage URI host.")
        account = host[: -len(suffix)]
        parts = parsed.path.lstrip("/").split("/", 1)
        container = parts[0] if parts else ""
        encoded_path = parts[1] if len(parts) > 1 else ""
    elif scheme == "abfss":
        if parsed.netloc.count("@") != 1:
            raise _invalid_identity("Malformed abfss URI.")
        encoded_container, host = parsed.netloc.split("@", 1)
        if ":" in host:
            raise _invalid_identity("Azure storage URI ports are not supported.")
        suffix = ".dfs.core.windows.net"
        if not host.lower().endswith(suffix):
            raise _invalid_identity("Unsupported Azure storage URI host.")
        account = host[: -len(suffix)]
        container = encoded_container
        encoded_path = parsed.path.lstrip("/")
    else:
        raise _invalid_identity("Unsupported Azure storage URI scheme.")

    account = account.lower()
    container = unquote(container).lower()
    if not _ACCOUNT_RE.fullmatch(account):
        raise _invalid_identity("Azure storage account name is malformed.")
    if container not in _SYSTEM_CONTAINERS and (not _CONTAINER_RE.fullmatch(container) or "--" in container):
        raise _invalid_identity("Azure storage container name is malformed.")
    return account, container, unquote(encoded_path)


def azure_uri_from_blob_name(account: str, container: str, blob_name: str) -> str:
    """Build a canonical URI from an SDK-decoded blob name, quoting exactly once."""
    account, container, _ = parse_azure_uri(f"az://{account}/{container}")
    encoded_path = quote(blob_name, safe="/-._~")
    return f"az://{account}/{container}/{encoded_path}" if encoded_path else f"az://{account}/{container}"


def canonicalize_azure_uri(uri: str) -> str:
    """Canonicalize a supported Azure Blob or DFS URI without credentials."""
    account, container, object_path = parse_azure_uri(uri)
    return azure_uri_from_blob_name(account, container, object_path)


def stable_source_id(source_type: str, root: str) -> str:
    """Derive a stable fallback source ID."""
    identity_root = canonicalize_azure_uri(root).rstrip("/") if source_type == "blob" else str(root)
    digest = hashlib.sha256(f"{source_type}\0{identity_root}".encode()).hexdigest()[:20]
    return f"{source_type}-{digest}"


def logical_artifact_id(source_id: str, logical_path: str) -> str:
    """Generate a location-independent ID for a newly discovered logical path."""
    normalized = normalize_logical_path(logical_path)
    return uuid.uuid5(_LOGICAL_ID_NAMESPACE, f"{source_id}\0{normalized}").hex


def split_alias(value: str, default_namespace: str = "artifact-id") -> tuple[str, str]:
    """Split a namespaced alias while retaining compatibility with opaque IDs."""
    if ":" not in value:
        return default_namespace, value
    namespace, alias = value.split(":", 1)
    if not namespace or not alias:
        raise _invalid_identity("Artifact aliases require non-empty namespace and value.")
    return namespace, alias


__all__ = [
    "azure_uri_from_blob_name",
    "canonicalize_azure_uri",
    "logical_artifact_id",
    "normalize_logical_path",
    "parse_azure_uri",
    "sanitize_uri_for_display",
    "split_alias",
    "stable_source_id",
]
