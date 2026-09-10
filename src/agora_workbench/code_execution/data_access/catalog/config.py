"""Pydantic models for catalog.yaml configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Optional
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
        data = value
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
            try:
                canonical = canonicalize_azure_uri(path)
            except ArtifactIdentityError:
                data["path"] = sanitized
                raise
            account, container, prefix = parse_azure_uri(canonical)
            data["path"] = azure_uri_from_blob_name(account, container, prefix)
        return data

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

    sources: list[SourceConfig] = Field(default_factory=list)
    search: SearchConfig = Field(default_factory=lambda: SearchConfig())

    @classmethod
    def from_yaml(cls, path: str | Path) -> "CatalogConfig":
        """Load configuration from a YAML file."""
        config_path = Path(path)
        if not config_path.exists():
            raise FileNotFoundError(f"Catalog config not found: {config_path}")
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        return cls.model_validate(raw)
