"""Compatibility wrappers for public artifact identity helpers."""

from __future__ import annotations

from agora_workbench.data_lake import identity as _identity
from agora_workbench.data_lake.errors import InvalidRequestError


class ArtifactIdentityError(ValueError):
    """Invalid artifact identity or Azure storage location."""


def _internal_call(function, *args):
    try:
        return function(*args)
    except InvalidRequestError as exc:
        raise ArtifactIdentityError(str(exc)) from exc


def normalize_logical_path(path: str) -> str:
    """Return a portable, source-relative POSIX path."""
    return _internal_call(_identity.normalize_logical_path, path)


def sanitize_uri_for_display(uri: str) -> str:
    """Remove credentials, query parameters, and fragments from a URI."""
    return _identity.sanitize_uri_for_display(uri)


def parse_azure_uri(uri: str) -> tuple[str, str, str]:
    """Parse a supported Azure URI into account, container, and decoded object path."""
    return _internal_call(_identity.parse_azure_uri, uri)


def azure_uri_from_blob_name(account: str, container: str, blob_name: str) -> str:
    """Build a canonical URI from an SDK-decoded blob name, quoting exactly once."""
    return _internal_call(_identity.azure_uri_from_blob_name, account, container, blob_name)


def canonicalize_azure_uri(uri: str) -> str:
    """Canonicalize supported Azure Blob/DFS URI forms without credentials."""
    return _internal_call(_identity.canonicalize_azure_uri, uri)


def stable_source_id(source_type: str, root: str) -> str:
    """Derive a stable fallback source ID."""
    return _internal_call(_identity.stable_source_id, source_type, root)


def logical_artifact_id(source_id: str, logical_path: str) -> str:
    """Generate a location-independent ID for a newly discovered logical path."""
    return _internal_call(_identity.logical_artifact_id, source_id, logical_path)


def split_alias(value: str, default_namespace: str = "artifact-id") -> tuple[str, str]:
    """Split a namespaced alias while retaining compatibility with opaque IDs."""
    return _internal_call(_identity.split_alias, value, default_namespace)


def is_reserved_provider_path(path: str) -> bool:
    """Return whether a path belongs to provider-managed state."""
    return _identity.is_reserved_provider_path(path)


def is_scan_excluded_path(path: str) -> bool:
    """Return whether scan discovery must prune a hidden or managed path."""
    return _identity.is_scan_excluded_path(path)
