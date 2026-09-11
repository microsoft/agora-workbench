"""Public artifact identity and storage-location normalization helpers."""

from __future__ import annotations

import hashlib
import posixpath
import re
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from .errors import InvalidRequestError

_LOGICAL_ID_NAMESPACE = uuid.UUID("37d400b9-1908-4a2d-a44b-b6a20e2492e8")
_ACCOUNT_RE = re.compile(r"^[a-z0-9]{3,24}$")
_CONTAINER_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])$")
_SYSTEM_CONTAINERS = frozenset({"$logs", "$root", "$web"})
RESERVED_PROVIDER_PREFIX = ".agora/"
RESERVED_MANIFEST_PATH = f"{RESERVED_PROVIDER_PREFIX}manifest.json"
RESERVED_OPERATIONS_PREFIX = f"{RESERVED_PROVIDER_PREFIX}operations/"
RESERVED_REVISIONS_PREFIX = f"{RESERVED_PROVIDER_PREFIX}revisions/"
RESERVED_RECEIPTS_PREFIX = f"{RESERVED_PROVIDER_PREFIX}receipts/"
_ENCODED_SEPARATOR_RE = re.compile(r"%(?:2f|5c)", re.IGNORECASE)
_MALFORMED_PERCENT_RE = re.compile(r"%(?![0-9a-fA-F]{2})")


def _invalid_identity(message: str) -> InvalidRequestError:
    return InvalidRequestError(message, operation="identity")


def normalize_logical_path(path: str) -> str:
    """Return a portable, source-relative POSIX path."""
    candidate = path.replace("\\", "/")
    if candidate.startswith("/") or re.match(r"^[a-zA-Z]:[\\/]", path):
        raise _invalid_identity("Artifact path must be source-relative.")
    normalized = posixpath.normpath(candidate)
    if normalized in {"", "."}:
        raise _invalid_identity("Artifact path must identify an object.")
    if normalized == ".." or normalized.startswith("../"):
        raise _invalid_identity("Artifact path must stay within its source.")
    return str(PurePosixPath(normalized))


def validate_azure_object_path(path: str, *, allow_empty: bool = False, allow_reserved: bool = False) -> str:
    """Validate one SDK-decoded Blob object path without changing its spelling."""
    if not path:
        if allow_empty:
            return ""
        raise _invalid_identity("Azure storage URI must identify an object.")
    if "\\" in path or "\x00" in path:
        raise _invalid_identity("Azure object path contains an ambiguous separator or NUL.")
    segments = path.split("/")
    path_segments = segments[:-1] if segments[-1] == "" else segments
    if any(segment in {"", ".", ".."} for segment in path_segments):
        raise _invalid_identity("Azure object path contains empty or dot segments.")
    if not allow_reserved and is_reserved_provider_path(path):
        raise _invalid_identity("Azure object path is reserved for provider metadata.")
    return path


def is_reserved_provider_path(path: str) -> bool:
    """Return whether a source-relative path belongs to provider-managed state."""
    normalized = path.replace("\\", "/").lstrip("/")
    return normalized == RESERVED_PROVIDER_PREFIX.rstrip("/") or normalized.startswith(RESERVED_PROVIDER_PREFIX)


def is_scan_excluded_path(path: str) -> bool:
    """Return whether scan discovery must prune a hidden or provider-managed path."""
    normalized = path.replace("\\", "/").strip("/")
    return is_reserved_provider_path(normalized) or any(segment.startswith(".") for segment in normalized.split("/"))


def validate_managed_revision_path(path: str) -> str:
    """Validate the narrow reserved storage namespace available to managed writes."""
    normalized = validate_azure_object_path(path, allow_reserved=True)
    if not normalized.startswith(RESERVED_REVISIONS_PREFIX):
        raise _invalid_identity("Managed storage path must be within the provider revisions prefix.")
    return normalized


@dataclass(frozen=True)
class AzureBlobScope:
    """Configured Azure account/container/prefix boundary for transfers."""

    account: str
    container: str
    prefix: str = ""

    def __post_init__(self) -> None:
        account, container, prefix = parse_azure_uri(
            azure_uri_from_blob_name(self.account, self.container, self.prefix)
        )
        object.__setattr__(self, "account", account)
        object.__setattr__(self, "container", container)
        object.__setattr__(
            self,
            "prefix",
            validate_azure_object_path(prefix.rstrip("/"), allow_empty=True, allow_reserved=True),
        )

    @classmethod
    def from_uri(cls, uri: str) -> "AzureBlobScope":
        """Build a validated scope from any supported Azure URI form."""
        account, container, prefix = parse_azure_uri(uri)
        return cls(account, container, prefix)

    def contains(self, account: str, container: str, object_path: str) -> bool:
        """Return whether an object lies on this scope's prefix boundary."""
        if (account, container) != (self.account, self.container):
            return False
        return not self.prefix or object_path == self.prefix or object_path.startswith(f"{self.prefix}/")


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
    if _MALFORMED_PERCENT_RE.search(uri):
        raise _invalid_identity("Azure storage URI contains malformed percent encoding.")
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

    if _ENCODED_SEPARATOR_RE.search(encoded_path):
        raise _invalid_identity("Azure object paths must not contain encoded separators.")
    account = account.lower()
    container = unquote(container).lower()
    if not _ACCOUNT_RE.fullmatch(account):
        raise _invalid_identity("Azure storage account name is malformed.")
    if container not in _SYSTEM_CONTAINERS and (not _CONTAINER_RE.fullmatch(container) or "--" in container):
        raise _invalid_identity("Azure storage container name is malformed.")
    object_path = unquote(encoded_path)
    validate_azure_object_path(object_path, allow_empty=True, allow_reserved=True)
    return account, container, object_path


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
    identity_root = canonicalize_azure_uri(root) if source_type == "blob" else str(root)
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
    "AzureBlobScope",
    "RESERVED_MANIFEST_PATH",
    "RESERVED_OPERATIONS_PREFIX",
    "RESERVED_PROVIDER_PREFIX",
    "RESERVED_RECEIPTS_PREFIX",
    "RESERVED_REVISIONS_PREFIX",
    "azure_uri_from_blob_name",
    "canonicalize_azure_uri",
    "logical_artifact_id",
    "is_scan_excluded_path",
    "is_reserved_provider_path",
    "normalize_logical_path",
    "parse_azure_uri",
    "sanitize_uri_for_display",
    "split_alias",
    "stable_source_id",
    "validate_azure_object_path",
    "validate_managed_revision_path",
]
