"""Pydantic models for catalog.yaml configuration."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field, model_validator

from .identity import (
    ArtifactIdentityError,
    azure_uri_from_blob_name,
    canonicalize_azure_uri,
    parse_azure_uri,
    sanitize_uri_for_display,
    split_alias,
)


class FileOverride(BaseModel):
    """Per-file metadata override within a source."""

    description: Optional[str] = None
    domain: Optional[str] = None
    artifact_id: Optional[str] = Field(None, description="Stable opaque artifact ID for this logical path")
    aliases: list[str] = Field(default_factory=list, description="Additional namespaced aliases for this artifact")

    @model_validator(mode="after")
    def _validate_identity_values(self):
        values = [("alias", alias) for alias in self.aliases]
        if self.artifact_id is not None:
            values.append(("artifact_id", self.artifact_id))
        for label, value in values:
            if not value.strip() or value != value.strip():
                raise ValueError(f"{label} must be non-empty and must not have surrounding whitespace")
            try:
                split_alias(value)
            except ArtifactIdentityError as exc:
                raise ValueError(f"Invalid {label}: {exc}") from exc
        return self


class DiscoveryMode(StrEnum):
    """How a source exposes artifacts to the catalog."""

    SCAN = "scan"
    MANIFEST = "manifest"


class SourceConfig(BaseModel):
    """A single data source (directory or blob prefix)."""

    path: str = Field(
        ...,
        description=(
            "Local path, az://account/container/prefix, Blob/DFS HTTPS URI, "
            "or abfss://container@account.dfs.core.windows.net/prefix"
        ),
    )
    source_id: Optional[str] = Field(
        None,
        description=(
            "Stable logical source ID. Configure this for local sources that must retain identity when their root moves."
        ),
    )
    discovery: DiscoveryMode = Field(
        default=DiscoveryMode.SCAN,
        description="Discovery mode: scan the source or load only authoritative manifest entries",
    )
    manifest: Optional[str] = Field(
        None,
        description=(
            "Manifest path. Local values may be absolute or relative to the source root; "
            "Blob values may be a full Azure URI or a blob name relative to the configured prefix."
        ),
    )
    max_stale_seconds: Optional[float] = Field(
        None,
        ge=0,
        description="Maximum age of the last valid manifest generation after refresh failure",
    )
    domain: Optional[str] = Field(None, description="Domain label for all files in this source")
    description: Optional[str] = Field(None, description="Default description for files without an explicit one")
    files: Optional[dict[str, FileOverride]] = Field(
        None, description="Per-file metadata overrides keyed by relative filename"
    )

    @model_validator(mode="before")
    @classmethod
    def _normalize_azure_path(cls, value):
        if not isinstance(value, dict) or not isinstance(value.get("path"), str):
            return value
        data = dict(value)
        path = data["path"]
        parsed = urlparse(path)
        host = (parsed.hostname or "").lower()
        azure_candidate = parsed.scheme.lower() in {"az", "abfss"} or (
            parsed.scheme.lower() in {"http", "https"}
            and (
                host == "blob.core.windows.net"
                or host == "dfs.core.windows.net"
                or host.endswith(".blob.core.windows.net")
                or host.endswith(".dfs.core.windows.net")
            )
        )
        if azure_candidate:
            sanitized = sanitize_uri_for_display(path)
            value["path"] = sanitized
            data["path"] = sanitized
            try:
                canonical = canonicalize_azure_uri(path)
            except ArtifactIdentityError:
                raise
            account, container, prefix = parse_azure_uri(canonical)
            data["path"] = azure_uri_from_blob_name(account, container, prefix)
        manifest = data.get("manifest")
        if isinstance(manifest, str) and "://" in manifest:
            sanitized_manifest = sanitize_uri_for_display(manifest)
            value["manifest"] = sanitized_manifest
            data["manifest"] = sanitized_manifest
            data["manifest"] = canonicalize_azure_uri(manifest)
        return data

    @model_validator(mode="after")
    def _validate_discovery(self):
        if not self.path.strip():
            raise ValueError("Catalog source path must be non-empty")
        if self.source_id is not None and (not self.source_id.strip() or self.source_id != self.source_id.strip()):
            raise ValueError("source_id must be non-empty and must not have surrounding whitespace")
        if self.discovery is DiscoveryMode.MANIFEST:
            if self.source_id is None:
                raise ValueError("Manifest sources require an explicit stable source_id")
            if not self.manifest or not self.manifest.strip():
                raise ValueError("Manifest sources require a manifest path")
            if self.source_type == "local" and "://" in self.manifest:
                raise ValueError("Local manifest sources require a local manifest path")
            if self.max_stale_seconds is not None and not isfinite(self.max_stale_seconds):
                raise ValueError("max_stale_seconds must be finite")
        elif self.manifest is not None:
            raise ValueError("manifest is only valid when discovery is 'manifest'")
        elif self.max_stale_seconds is not None:
            raise ValueError("max_stale_seconds is only valid when discovery is 'manifest'")
        return self

    @property
    def source_type(self) -> str:
        """Infer storage type from path prefix."""
        return "blob" if self.path.startswith("az://") else "local"


class SearchConfig(BaseModel):
    """Search/embedding configuration."""

    embedding_model: str = Field(
        default="none",
        description=(
            "Embedding model for vector search: 'none' (keyword/BM25 only — the "
            "default, no extra setup) or 'azure-openai' (requires the endpoint / "
            "deployment below)."
        ),
    )
    azure_openai_endpoint: Optional[str] = Field(
        None, description="Azure OpenAI endpoint (required if embedding_model is 'azure-openai')"
    )
    azure_openai_deployment: Optional[str] = Field(
        None, description="Azure OpenAI deployment name (required if embedding_model is 'azure-openai')"
    )
    embedding_dimensions: Optional[int] = Field(
        default=None,
        gt=0,
        description=(
            "Optional embedding dimensions. None uses the deployment's service default; "
            "when set, the value must be supported by the deployment and match CatalogDB.vec_dimensions."
        ),
    )
    hybrid_alpha: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Weight for FTS score in hybrid ranking (1-alpha for vector)",
    )


class CatalogConfig(BaseModel):
    """Top-level catalog.yaml configuration."""

    version: int = Field(default=1, description="Catalog source configuration version")
    sources: list[SourceConfig] = Field(default_factory=list)
    search: SearchConfig = Field(default_factory=lambda: SearchConfig())

    @model_validator(mode="after")
    def _validate_version(self):
        if self.version != 1:
            raise ValueError(f"Unsupported catalog configuration version: {self.version}")
        return self

    @classmethod
    def from_yaml(cls, path: str | Path) -> "CatalogConfig":
        """Load configuration from a YAML file."""
        config_path = Path(path)
        if not config_path.exists():
            raise FileNotFoundError(f"Catalog config not found: {config_path}")
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        return cls.model_validate(raw)

    @classmethod
    def convert(cls, raw: dict[str, Any]) -> tuple["CatalogConfig", dict[str, object]]:
        """Convert and report the existing public catalog format without storage access."""
        config = cls.model_validate(raw)
        sources = []
        for source in config.sources:
            sources.append(
                {
                    "source_id": source.source_id
                    or (
                        "derived at load time from the canonical source root"
                        if source.discovery is DiscoveryMode.SCAN
                        else None
                    ),
                    "source_type": source.source_type,
                    "discovery": source.discovery.value,
                    "path": source.path,
                    "manifest": source.manifest,
                    "max_stale_seconds": source.max_stale_seconds,
                    "files_are_metadata_overrides": source.files is not None,
                    "configuration_valid": True,
                    "manifest_checked": False,
                    "manifest_content_valid": None,
                }
            )
        return config, {
            "version": config.version,
            "configuration_valid": True,
            "manifest_checked": False,
            "manifest_content_valid": None,
            "storage_accessed": False,
            "sources": sources,
        }


@dataclass(frozen=True)
class CatalogConfigConversionReport:
    """Result of converting legacy catalog YAML to explicit versioned source configuration."""

    source: Path
    destination: Path | None
    changed: bool
    written: bool
    rendered_yaml: str
    summary: dict[str, object]


def convert_catalog_config(
    source: str | Path,
    destination: str | Path | None = None,
    *,
    dry_run: bool = True,
) -> CatalogConfigConversionReport:
    """Validate and render explicit version-1 catalog configuration.

    Dry-run is the default and never writes. A non-dry-run conversion requires a
    separate destination so an existing public configuration is not overwritten
    implicitly.
    """
    source_path = Path(source)
    if not source_path.exists():
        raise FileNotFoundError(f"Catalog config not found: {source_path}")
    raw = yaml.safe_load(source_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("Catalog configuration must contain a YAML object")
    config, summary = CatalogConfig.convert(raw)
    explicit_sources = []
    for source_config in config.sources:
        rendered_source = source_config.model_dump(
            mode="json",
            exclude_none=True,
            exclude_defaults=True,
        )
        rendered_source["path"] = source_config.path
        rendered_source["discovery"] = source_config.discovery.value
        explicit_sources.append(rendered_source)
    explicit: dict[str, object] = {
        "version": config.version,
        "sources": explicit_sources,
    }
    if "search" in raw or config.search != SearchConfig():
        explicit["search"] = config.search.model_dump(
            mode="json",
            exclude_none=True,
            exclude_defaults=True,
        )
    rendered = yaml.safe_dump(explicit, sort_keys=False)
    destination_path = Path(destination) if destination is not None else None
    if not dry_run:
        if destination_path is None:
            raise ValueError("A destination is required when dry_run is False")
        if destination_path.resolve() == source_path.resolve():
            raise ValueError("Catalog conversion destination must differ from the source")
        destination_path.write_text(rendered, encoding="utf-8")
    return CatalogConfigConversionReport(
        source=source_path,
        destination=destination_path,
        changed=explicit != raw,
        written=not dry_run,
        rendered_yaml=rendered,
        summary=summary,
    )
