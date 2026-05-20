"""Pydantic models for catalog.yaml configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field


class FileOverride(BaseModel):
    """Per-file metadata override within a source."""

    description: Optional[str] = None
    domain: Optional[str] = None


class SourceConfig(BaseModel):
    """A single data source (directory or blob prefix)."""

    path: str = Field(
        ...,
        description="Local path, az://account/container/prefix, or https://<account>.blob.core.windows.net/container/prefix",
    )
    domain: Optional[str] = Field(None, description="Domain label for all files in this source")
    description: Optional[str] = Field(None, description="Default description for files without an explicit one")
    files: Optional[dict[str, FileOverride]] = Field(
        None, description="Per-file metadata overrides keyed by relative filename"
    )

    @property
    def source_type(self) -> str:
        """Infer storage type from path prefix."""
        if self.path.startswith("az://") or ".blob.core.windows.net" in self.path:
            return "blob"
        return "local"


class SearchConfig(BaseModel):
    """Search/embedding configuration."""

    embedding_model: str = Field(
        default="nomic-ai/nomic-embed-text-v1.5",
        description="Model name: a sentence-transformers model ID or 'azure-openai'",
    )
    azure_openai_endpoint: Optional[str] = Field(
        None, description="Azure OpenAI endpoint (required if embedding_model is 'azure-openai')"
    )
    azure_openai_deployment: Optional[str] = Field(
        None, description="Azure OpenAI deployment name (required if embedding_model is 'azure-openai')"
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
    search: SearchConfig = Field(default_factory=SearchConfig)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "CatalogConfig":
        """Load configuration from a YAML file."""
        config_path = Path(path)
        if not config_path.exists():
            raise FileNotFoundError(f"Catalog config not found: {config_path}")
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        return cls.model_validate(raw)
