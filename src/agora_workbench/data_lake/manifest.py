"""Versioned, storage-neutral catalog manifest records."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .errors import InvalidRequestError
from .identity import normalize_logical_path, split_alias

MANIFEST_VERSION = 1
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_MANIFEST_ARTIFACTS = 10_000


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

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", normalize_logical_path(self.path))
        object.__setattr__(self, "aliases", tuple(self.aliases))
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
            "aliases",
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
        return cls(**kwargs)  # type: ignore[arg-type]


@dataclass(frozen=True)
class CatalogManifest:
    """An authoritative manifest generation."""

    version: int
    generation: int
    artifacts: tuple[ManifestArtifact, ...]

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "CatalogManifest":
        unknown = set(value) - {"version", "generation", "artifacts"}
        if unknown:
            raise _invalid(f"Unknown manifest fields: {', '.join(sorted(unknown))}")
        version = value.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or version != MANIFEST_VERSION:
            raise _invalid(f"Unsupported manifest version: {version!r}")
        generation = value.get("generation")
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
            raise _invalid("Manifest generation must be a positive integer.")
        artifacts = value.get("artifacts")
        if not isinstance(artifacts, Sequence) or isinstance(artifacts, (str, bytes)):
            raise _invalid("Manifest artifacts must be a list.")
        if len(artifacts) > MAX_MANIFEST_ARTIFACTS:
            raise _invalid(f"Manifest contains more than {MAX_MANIFEST_ARTIFACTS} artifacts.")
        parsed = tuple(
            ManifestArtifact.from_mapping(item) if isinstance(item, Mapping) else (_raise_artifact_object())
            for item in artifacts
        )
        paths = [artifact.path for artifact in parsed]
        if len(paths) != len(set(paths)):
            raise _invalid("Manifest artifact paths must be unique.")
        artifact_ids = [artifact.artifact_id for artifact in parsed if artifact.artifact_id is not None]
        if len(artifact_ids) != len(set(artifact_ids)):
            raise _invalid("Manifest artifact_id values must be unique within a source.")
        aliases = [alias for artifact in parsed for alias in artifact.aliases]
        if len(aliases) != len(set(aliases)):
            raise _invalid("Manifest aliases must be unique within a source.")
        return cls(version=MANIFEST_VERSION, generation=generation, artifacts=parsed)


def _raise_artifact_object() -> ManifestArtifact:
    raise _invalid("Each manifest artifact must be an object.")


__all__ = [
    "MANIFEST_VERSION",
    "MAX_MANIFEST_ARTIFACTS",
    "MAX_MANIFEST_BYTES",
    "CatalogManifest",
    "ManifestArtifact",
]
