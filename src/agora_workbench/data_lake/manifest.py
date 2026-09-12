"""Versioned, storage-neutral catalog manifest records."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum

from .errors import InvalidRequestError
from .identity import is_reserved_provider_path, normalize_logical_path, split_alias, validate_managed_revision_path

MANIFEST_VERSION = 1
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_MANIFEST_ARTIFACTS = 10_000


class ManifestOwnership(StrEnum):
    """Ownership of bytes referenced by a manifest revision."""

    MANAGED = "managed"
    EXTERNAL = "external"


@dataclass(frozen=True)
class ManifestProvenance:
    """Audit provenance attached to a managed revision."""

    operation_id: str
    kind: str
    created_at: str
    caller_id: str | None = None
    source_uri: str | None = None
    session_id: str | None = None
    output_name: str | None = None

    def __post_init__(self) -> None:
        if not self.operation_id or not self.kind:
            raise _invalid("Manifest provenance requires operation_id and kind.")
        try:
            datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise _invalid("Manifest provenance created_at must be an ISO-8601 timestamp.") from exc

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "ManifestProvenance":
        allowed = {"operation_id", "kind", "created_at", "caller_id", "source_uri", "session_id", "output_name"}
        unknown = set(value) - allowed
        if unknown:
            raise _invalid(f"Unknown manifest provenance fields: {', '.join(sorted(unknown))}")
        if any(value.get(key) is not None and not isinstance(value.get(key), str) for key in allowed):
            raise _invalid("Manifest provenance fields must be strings.")
        return cls(**dict(value))  # type: ignore[arg-type]


@dataclass(frozen=True)
class ManifestRevision:
    """One immutable physical revision retained by the manifest."""

    revision_id: str
    storage_path: str
    content_revision: str
    created_at: str
    operation_id: str
    ownership: ManifestOwnership
    checksum_sha256: str | None = None
    size_bytes: int | None = None
    version_token: str | None = None
    committed_generation: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.ownership, ManifestOwnership):
            raise _invalid("Manifest revision ownership is invalid.")
        if not self.revision_id or not self.content_revision or not self.operation_id:
            raise _invalid("Manifest revision identity fields must be non-empty.")
        object.__setattr__(self, "storage_path", validate_managed_revision_path(self.storage_path))
        if self.size_bytes is not None and self.size_bytes < 0:
            raise _invalid("Manifest revision size_bytes must be non-negative.")
        if self.committed_generation is not None and self.committed_generation < 1:
            raise _invalid("Manifest revision committed_generation must be positive.")
        try:
            datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise _invalid("Manifest revision created_at must be an ISO-8601 timestamp.") from exc

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "ManifestRevision":
        allowed = {
            "revision_id",
            "storage_path",
            "content_revision",
            "created_at",
            "operation_id",
            "ownership",
            "checksum_sha256",
            "size_bytes",
            "version_token",
            "committed_generation",
        }
        unknown = set(value) - allowed
        if unknown:
            raise _invalid(f"Unknown manifest revision fields: {', '.join(sorted(unknown))}")
        for field_name in (
            "revision_id",
            "storage_path",
            "content_revision",
            "created_at",
            "operation_id",
            "checksum_sha256",
            "version_token",
        ):
            field_value = value.get(field_name)
            if field_value is not None and not isinstance(field_value, str):
                raise _invalid(f"Manifest revision {field_name} must be a string.")
        size_bytes = value.get("size_bytes")
        if size_bytes is not None and (not isinstance(size_bytes, int) or isinstance(size_bytes, bool)):
            raise _invalid("Manifest revision size_bytes must be an integer.")
        committed_generation = value.get("committed_generation")
        if committed_generation is not None and (
            not isinstance(committed_generation, int) or isinstance(committed_generation, bool)
        ):
            raise _invalid("Manifest revision committed_generation must be an integer.")
        try:
            ownership = ManifestOwnership(value.get("ownership"))
        except (TypeError, ValueError) as exc:
            raise _invalid("Manifest revision ownership is invalid.") from exc
        kwargs = dict(value)
        kwargs["ownership"] = ownership
        return cls(**kwargs)  # type: ignore[arg-type]


@dataclass(frozen=True)
class ManifestRemoval:
    """Durable evidence of one committed tombstone operation."""

    operation_id: str
    generation: int
    revision_id: str | None
    storage_path: str | None
    garbage_collect: bool
    revisions: tuple[ManifestRevision, ...] = ()

    def __post_init__(self) -> None:
        if not self.operation_id:
            raise _invalid("Manifest removal operation_id must be non-empty.")
        if self.generation < 1:
            raise _invalid("Manifest removal generation must be positive.")
        if self.storage_path is not None:
            object.__setattr__(self, "storage_path", validate_managed_revision_path(self.storage_path))
        object.__setattr__(self, "revisions", tuple(self.revisions))

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "ManifestRemoval":
        allowed = {"operation_id", "generation", "revision_id", "storage_path", "garbage_collect", "revisions"}
        unknown = set(value) - allowed
        if unknown:
            raise _invalid(f"Unknown manifest removal fields: {', '.join(sorted(unknown))}")
        if not isinstance(value.get("operation_id"), str):
            raise _invalid("Manifest removal operation_id must be a string.")
        generation = value.get("generation")
        if not isinstance(generation, int) or isinstance(generation, bool):
            raise _invalid("Manifest removal generation must be an integer.")
        for field_name in ("revision_id", "storage_path"):
            field_value = value.get(field_name)
            if field_value is not None and not isinstance(field_value, str):
                raise _invalid(f"Manifest removal {field_name} must be a string.")
        if not isinstance(value.get("garbage_collect"), bool):
            raise _invalid("Manifest removal garbage_collect must be a boolean.")
        revisions = value.get("revisions", ())
        if not isinstance(revisions, Sequence) or isinstance(revisions, (str, bytes)):
            raise _invalid("Manifest removal revisions must be a list.")
        kwargs = dict(value)
        kwargs["revisions"] = tuple(
            ManifestRevision.from_mapping(item) if isinstance(item, Mapping) else (_raise_revision_object())
            for item in revisions
        )
        return cls(**kwargs)  # type: ignore[arg-type]


def _invalid(message: str) -> InvalidRequestError:
    return InvalidRequestError(message, operation="manifest")


@dataclass(frozen=True)
class ManifestArtifact:
    """One authoritative artifact registration."""

    path: str
    artifact_id: str | None = None
    name: str | None = None
    description: str | None = None
    domain: str | None = None
    media_type: str | None = None
    size_bytes: int | None = None
    content_revision: str | None = None
    metadata_revision: str | None = None
    checksum_sha256: str | None = None
    aliases: tuple[str, ...] = ()
    storage_path: str | None = None
    revision_id: str | None = None
    revisions: tuple[ManifestRevision, ...] = ()
    removals: tuple[ManifestRemoval, ...] = ()
    ownership: ManifestOwnership | None = None
    deleted_at: str | None = None
    provenance: ManifestProvenance | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", normalize_logical_path(self.path))
        if is_reserved_provider_path(self.path):
            raise _invalid("Manifest artifact logical path is reserved for provider metadata.")
        object.__setattr__(self, "aliases", tuple(self.aliases))
        object.__setattr__(self, "revisions", tuple(self.revisions))
        object.__setattr__(self, "removals", tuple(self.removals))
        if self.storage_path is not None:
            object.__setattr__(self, "storage_path", validate_managed_revision_path(self.storage_path))
        if self.artifact_id is not None:
            if not self.artifact_id.strip() or self.artifact_id != self.artifact_id.strip():
                raise _invalid("Manifest artifact artifact_id must be non-empty and have no surrounding whitespace.")
            split_alias(self.artifact_id)
        for alias in self.aliases:
            if not alias.strip() or alias != alias.strip():
                raise _invalid("Manifest artifact aliases must be non-empty and have no surrounding whitespace.")
            split_alias(alias)
        if self.size_bytes is not None and self.size_bytes < 0:
            raise _invalid("Manifest artifact size_bytes must be non-negative.")
        if self.deleted_at is not None:
            try:
                datetime.fromisoformat(self.deleted_at.replace("Z", "+00:00"))
            except ValueError as exc:
                raise _invalid("Manifest artifact deleted_at must be an ISO-8601 timestamp.") from exc
        revision_ids = [revision.revision_id for revision in self.revisions]
        if len(revision_ids) != len(set(revision_ids)):
            raise _invalid("Manifest artifact revision IDs must be unique.")
        if self.revision_id is not None and self.revision_id not in revision_ids:
            raise _invalid("Manifest artifact current revision must be retained.")
        if self.revision_id is not None and self.storage_path is None:
            raise _invalid("Manifest artifact current revision requires storage_path.")
        if self.revision_id is not None:
            current = next(revision for revision in self.revisions if revision.revision_id == self.revision_id)
            if current.storage_path != self.storage_path:
                raise _invalid("Manifest artifact storage_path must match its current revision.")
            if self.content_revision is not None and current.content_revision != self.content_revision:
                raise _invalid("Manifest artifact content_revision must match its current revision.")
            if self.checksum_sha256 is not None and current.checksum_sha256 != self.checksum_sha256:
                raise _invalid("Manifest artifact checksum must match its current revision.")
            if self.ownership is not None and current.ownership is not self.ownership:
                raise _invalid("Manifest artifact ownership must match its current revision.")
        removal_ids = [removal.operation_id for removal in self.removals]
        if len(removal_ids) != len(set(removal_ids)):
            raise _invalid("Manifest artifact removal operation IDs must be unique.")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "ManifestArtifact":
        allowed = {
            "path",
            "artifact_id",
            "name",
            "description",
            "domain",
            "media_type",
            "size_bytes",
            "content_revision",
            "metadata_revision",
            "checksum_sha256",
            "storage_path",
            "revision_id",
            "deleted_at",
            "aliases",
            "storage_path",
            "revision_id",
            "revisions",
            "removals",
            "ownership",
            "deleted_at",
            "provenance",
        }
        unknown = set(value) - allowed
        if unknown:
            raise _invalid(f"Unknown manifest artifact fields: {', '.join(sorted(unknown))}")
        path = value.get("path")
        if not isinstance(path, str):
            raise _invalid("Manifest artifact path must be a string.")
        for field_name in (
            "artifact_id",
            "name",
            "description",
            "domain",
            "media_type",
            "content_revision",
            "metadata_revision",
            "checksum_sha256",
        ):
            field_value = value.get(field_name)
            if field_value is not None and not isinstance(field_value, str):
                raise _invalid(f"Manifest artifact {field_name} must be a string.")
        size_bytes = value.get("size_bytes")
        if size_bytes is not None and (not isinstance(size_bytes, int) or isinstance(size_bytes, bool)):
            raise _invalid("Manifest artifact size_bytes must be an integer.")
        aliases = value.get("aliases", ())
        if not isinstance(aliases, Sequence) or isinstance(aliases, (str, bytes)):
            raise _invalid("Manifest artifact aliases must be a list.")
        if any(not isinstance(alias, str) for alias in aliases):
            raise _invalid("Manifest artifact aliases must contain only strings.")
        if len(aliases) != len(set(aliases)):
            raise _invalid(f"Manifest artifact {path!r} contains duplicate aliases.")
        kwargs = dict(value)
        kwargs["aliases"] = tuple(aliases)
        revisions = value.get("revisions", ())
        if not isinstance(revisions, Sequence) or isinstance(revisions, (str, bytes)):
            raise _invalid("Manifest artifact revisions must be a list.")
        kwargs["revisions"] = tuple(
            ManifestRevision.from_mapping(item) if isinstance(item, Mapping) else (_raise_revision_object())
            for item in revisions
        )
        removals = value.get("removals", ())
        if not isinstance(removals, Sequence) or isinstance(removals, (str, bytes)):
            raise _invalid("Manifest artifact removals must be a list.")
        kwargs["removals"] = tuple(
            ManifestRemoval.from_mapping(item) if isinstance(item, Mapping) else (_raise_removal_object())
            for item in removals
        )
        ownership = value.get("ownership")
        if ownership is not None:
            try:
                kwargs["ownership"] = ManifestOwnership(ownership)
            except (TypeError, ValueError) as exc:
                raise _invalid("Manifest artifact ownership is invalid.") from exc
        provenance = value.get("provenance")
        if provenance is not None:
            if not isinstance(provenance, Mapping):
                raise _invalid("Manifest artifact provenance must be an object.")
            kwargs["provenance"] = ManifestProvenance.from_mapping(provenance)
        return cls(**kwargs)  # type: ignore[arg-type]

    def to_mapping(self) -> dict[str, object]:
        """Serialize the artifact using the stable manifest field names."""
        value = asdict(self)
        value["ownership"] = self.ownership.value if self.ownership is not None else None
        value["revisions"] = [
            {**asdict(revision), "ownership": revision.ownership.value} for revision in self.revisions
        ]
        return {key: item for key, item in value.items() if item is not None and item != ()}


@dataclass(frozen=True)
class CatalogManifest:
    """An authoritative manifest generation."""

    version: int
    generation: int
    artifacts: tuple[ManifestArtifact, ...]
    commit_fences: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "CatalogManifest":
        unknown = set(value) - {"version", "generation", "artifacts", "commit_fences"}
        if unknown:
            raise _invalid(f"Unknown manifest fields: {', '.join(sorted(unknown))}")
        version = value.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or version != MANIFEST_VERSION:
            raise _invalid(f"Unsupported manifest version: {version!r}")
        generation = value.get("generation")
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
            raise _invalid("Manifest generation must be a non-negative integer.")
        artifacts = value.get("artifacts")
        if not isinstance(artifacts, Sequence) or isinstance(artifacts, (str, bytes)):
            raise _invalid("Manifest artifacts must be a list.")
        if len(artifacts) > MAX_MANIFEST_ARTIFACTS:
            raise _invalid(f"Manifest contains more than {MAX_MANIFEST_ARTIFACTS} artifacts.")
        parsed = tuple(
            ManifestArtifact.from_mapping(item) if isinstance(item, Mapping) else (_raise_artifact_object())
            for item in artifacts
        )
        if generation == 0 and parsed:
            raise _invalid("Manifest generation zero cannot contain artifacts.")
        paths = [artifact.path for artifact in parsed]
        if len(paths) != len(set(paths)):
            raise _invalid("Manifest artifact paths must be unique.")
        artifact_ids = [artifact.artifact_id for artifact in parsed if artifact.artifact_id is not None]
        if len(artifact_ids) != len(set(artifact_ids)):
            raise _invalid("Manifest artifact_id values must be unique within a source.")
        aliases = [alias for artifact in parsed for alias in artifact.aliases]
        if len(aliases) != len(set(aliases)):
            raise _invalid("Manifest aliases must be unique within a source.")
        commit_fences = value.get("commit_fences", ())
        if not isinstance(commit_fences, Sequence) or isinstance(commit_fences, (str, bytes)):
            raise _invalid("Manifest commit_fences must be a list.")
        if any(not isinstance(fence, str) or not fence for fence in commit_fences):
            raise _invalid("Manifest commit_fences must contain non-empty strings.")
        if len(commit_fences) != len(set(commit_fences)):
            raise _invalid("Manifest commit_fences must be unique.")
        return cls(
            version=MANIFEST_VERSION,
            generation=generation,
            artifacts=parsed,
            commit_fences=tuple(commit_fences),
        )

    def to_mapping(self) -> dict[str, object]:
        """Serialize a canonical JSON-compatible manifest mapping."""
        return {
            "version": self.version,
            "generation": self.generation,
            "artifacts": [artifact.to_mapping() for artifact in self.artifacts],
            **({"commit_fences": list(self.commit_fences)} if self.commit_fences else {}),
        }


def _raise_artifact_object() -> ManifestArtifact:
    raise _invalid("Each manifest artifact must be an object.")


def _raise_revision_object() -> ManifestRevision:
    raise _invalid("Each manifest revision must be an object.")


def _raise_removal_object() -> ManifestRemoval:
    raise _invalid("Each manifest removal must be an object.")


__all__ = [
    "MANIFEST_VERSION",
    "MAX_MANIFEST_ARTIFACTS",
    "MAX_MANIFEST_BYTES",
    "CatalogManifest",
    "ManifestArtifact",
    "ManifestOwnership",
    "ManifestProvenance",
    "ManifestRemoval",
    "ManifestRevision",
]
