"""Pydantic models for data-lake manifests.

This module centralizes both manifest families used by the data_lake package:

- ``IngestionManifest`` for the step 3-6 ingestion pipeline
- ``UtilityManifest`` for standalone catalog audit/update operations
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional, Tuple

import yaml
from pydantic import BaseModel, Field, model_validator


class YamlBackedModel(BaseModel):
    """Small helper for manifest models serialized to YAML."""

    @classmethod
    def from_yaml(cls, path: str | Path):
        """Load and validate a manifest from a YAML file."""
        with open(path, "r") as fh:
            raw = yaml.safe_load(fh)
        return cls.model_validate(raw)

    def to_yaml(self, path: str | Path) -> None:
        """Serialize the manifest to a YAML file."""
        with open(path, "w") as fh:
            yaml.dump(
                self.model_dump(mode="json", exclude_none=True),
                fh,
                default_flow_style=False,
                sort_keys=False,
            )


# ---------------------------------------------------------------------------
# Ingestion manifest sub-models
# ---------------------------------------------------------------------------


class SourceConfig(BaseModel):
    """Azure Blob Storage data source coordinates."""

    storage_account: str = Field(..., description="Name of the Azure Storage account (e.g. 'grid0eastus2').")
    resource_group: str = Field(..., description="Azure resource group that contains the storage account.")
    subscription_id: str = Field(..., description="Azure subscription ID for the storage account.")
    container: str = Field(..., description="Blob container name to ingest (e.g. 'demo').")
    source_id: Optional[str] = Field(
        None,
        description=(
            "Unique identifier used for the AI Search data source / indexer. "
            "Defaults to '{storage_account}-{container}' if omitted."
        ),
    )
    managed_identity_id: Optional[str] = Field(
        None,
        description=(
            "Azure resource ID of the user-assigned managed identity for storage access. "
            "Falls back to the DEFAULT_IDENTITY_RESOURCE_ID env var."
        ),
    )
    container_query: Optional[str] = Field(
        None,
        description="Optional OData query to filter blobs within the container.",
    )
    included_extensions: Optional[List[str]] = Field(
        None,
        description="File extensions to include (e.g. ['.csv', '.nc']). None = all.",
    )
    excluded_extensions: Optional[List[str]] = Field(
        None,
        description="File extensions to exclude (e.g. ['.zip', '.tar']).",
    )

    @model_validator(mode="after")
    def _set_defaults(self) -> "SourceConfig":
        if self.source_id is None:
            self.source_id = f"{self.storage_account}-{self.container}"
        if self.managed_identity_id is None:
            self.managed_identity_id = os.getenv("DEFAULT_IDENTITY_RESOURCE_ID")
        return self


class GovernanceConfig(BaseModel):
    """Purview governance settings."""

    purview_account: str = Field(..., description="Microsoft Purview account name (e.g. 'agora-purview').")
    collection: Optional[str] = Field(
        None,
        description=(
            "Default Purview collection. Individual datasets can override this with their own collection field."
        ),
    )


class ArtifactDescription(BaseModel):
    """A user-supplied semantic description for a single artifact."""

    path: str = Field(
        ...,
        description=(
            "Artifact path relative to the subfolder. Can be a file "
            "(e.g. 'results.csv') or a folder (e.g. 'simulation_outputs/'). "
            "Folder paths should end with '/'."
        ),
    )
    description: str = Field(
        ...,
        description="Human-readable description of this artifact.",
    )


class DataConfigMulti(BaseModel):
    """A single dataset entry in the ``datasets`` list."""

    subfolder: Optional[str] = Field(
        None,
        description=(
            "Subfolder path within the container to scope this dataset to "
            "(e.g. 'whr' or 'datasets/whr'). Omit to target the container root."
        ),
    )
    collection: Optional[str] = Field(
        None,
        description=(
            "Purview collection to place scanned entities into. Falls back to governance.collection if omitted."
        ),
    )
    description: str = Field(
        ...,
        description="High-level description of this dataset / subfolder.",
    )
    artifacts: List[ArtifactDescription] = Field(
        default_factory=list,
        description="Per-artifact semantic descriptions within this dataset.",
    )


class DataConfigSingle(BaseModel):
    """Internal model returned by ``iter_subfolders``."""

    description: str = Field(
        ...,
        description=(
            "High-level description of the subfolder being ingested. "
            "This is applied to the subfolder (or container) entity in Purview."
        ),
    )
    artifacts: List[ArtifactDescription] = Field(
        default_factory=list,
        description=("Descriptions for the individual artifacts (data files) within this subfolder."),
    )


class SearchConfig(BaseModel):
    """Azure AI Search service settings."""

    search_service: Optional[str] = Field(
        None,
        description=(
            "Azure AI Search service name (e.g. 'agora-ai-search'). Falls back to DATA_LAKE_SEARCH_NAME env var."
        ),
    )
    blob_details_index: Optional[str] = Field(
        None,
        description=(
            "Name of the blob-details index. Falls back to DATA_LAKE_BLOB_DETAILS_INDEX env var, then 'blob-details'."
        ),
    )
    artifact_registry_index: Optional[str] = Field(
        None,
        description=(
            "Name of the artifact registry index. "
            "Falls back to DATA_LAKE_CATALOG_INDEX_NAME env var, then 'artifact-registry'."
        ),
    )

    @model_validator(mode="after")
    def _resolve_env(self) -> "SearchConfig":
        if self.search_service is None:
            self.search_service = os.getenv("DATA_LAKE_SEARCH_NAME")
            if self.search_service is None:
                raise ValueError(
                    "search_service is required. Set it in the manifest or via DATA_LAKE_SEARCH_NAME env var."
                )
        if self.blob_details_index is None:
            self.blob_details_index = os.getenv("DATA_LAKE_BLOB_DETAILS_INDEX", "blob-details")
        if self.artifact_registry_index is None:
            self.artifact_registry_index = os.getenv("DATA_LAKE_CATALOG_INDEX_NAME", "artifact-registry")
        return self


class EmbeddingConfig(BaseModel):
    """Azure OpenAI embedding configuration for the sync step."""

    azure_openai_endpoint: Optional[str] = Field(
        None,
        description=("Base Azure OpenAI endpoint URL. Falls back to DATA_LAKE_VECTORIZER_ENDPOINT env var."),
    )
    azure_openai_deployment: Optional[str] = Field(
        None,
        description=(
            "Embedding model deployment name. "
            "Falls back to DATA_LAKE_VECTORIZER_DEPLOYMENT env var, "
            "then defaults to 'text-embedding-3-large'."
        ),
    )

    @model_validator(mode="after")
    def _resolve_env(self) -> "EmbeddingConfig":
        if self.azure_openai_endpoint is None:
            self.azure_openai_endpoint = os.getenv("DATA_LAKE_VECTORIZER_ENDPOINT")
        if self.azure_openai_deployment is None:
            self.azure_openai_deployment = os.getenv("DATA_LAKE_VECTORIZER_DEPLOYMENT", "text-embedding-3-large")
        return self


class IngestionManifest(YamlBackedModel):
    """Top-level ingestion manifest."""

    version: str = Field("1", description="Manifest schema version.")
    source: SourceConfig
    governance: GovernanceConfig
    datasets: List[DataConfigMulti] = Field(
        ...,
        description="List of datasets (subfolders) to ingest.",
    )
    search: SearchConfig = Field(default_factory=lambda: SearchConfig())  # type: ignore[call-arg]
    embedding: EmbeddingConfig = Field(default_factory=lambda: EmbeddingConfig())  # type: ignore[call-arg]

    def iter_subfolders(self) -> List[Tuple[Optional[str], DataConfigSingle, str]]:
        """Yield ``(subfolder, data, collection)`` for each dataset entry."""
        result: List[Tuple[Optional[str], DataConfigSingle, str]] = []
        for ds in self.datasets:
            collection = ds.collection or self.governance.collection
            if collection is None:
                label = ds.subfolder or "(root)"
                raise ValueError(f"Dataset '{label}' has no collection and governance.collection is also unset.")
            data = DataConfigSingle(
                description=ds.description,
                artifacts=ds.artifacts,
            )
            subfolder = ds.subfolder.strip("/") if ds.subfolder else None
            result.append((subfolder, data, collection))
        return result


# ---------------------------------------------------------------------------
# Utility manifest sub-models
# ---------------------------------------------------------------------------


class ArtifactRegistryQueryConfig(BaseModel):
    """Parameters for an artifact-registry query."""

    search_service: str = Field(..., description="Azure AI Search service name (e.g. 'agora-ai-search').")
    index_name: str = Field(
        "artifact-registry",
        description="Artifact registry index or alias name.",
    )
    filter_expression: Optional[str] = Field(
        None,
        description="Optional OData filter expression.",
    )
    top: Optional[int] = Field(
        None,
        ge=1,
        description="Maximum number of documents to return. None fetches all.",
    )
    select_fields: Optional[List[str]] = Field(
        None,
        description="Optional list of fields to include in the result payload.",
    )
    output_path: Optional[str] = Field(
        None,
        description="Optional JSON file path to write query results to.",
    )


class PurviewEntityUpdateConfig(BaseModel):
    """A targeted update to a single Purview entity."""

    qualified_name: str = Field(
        ...,
        description="Qualified name of the Purview entity, typically the blob URL.",
    )
    new_name: Optional[str] = Field(
        None,
        description="New display name to assign.",
    )
    new_description: Optional[str] = Field(
        None,
        description="New Purview userDescription value.",
    )

    @model_validator(mode="after")
    def _require_change(self) -> "PurviewEntityUpdateConfig":
        if self.new_name is None and self.new_description is None:
            raise ValueError("Each entity update must provide new_name and/or new_description.")
        return self


class UtilityManifest(YamlBackedModel):
    """Top-level manifest for standalone utility operations."""

    version: str = Field("1", description="Manifest schema version.")
    purview_account: Optional[str] = Field(
        None,
        description="Purview account name required when entity_updates are present.",
    )
    registry_query: Optional[ArtifactRegistryQueryConfig] = Field(
        None,
        description="Optional artifact-registry query to run before updates.",
    )
    entity_updates: List[PurviewEntityUpdateConfig] = Field(
        default_factory=list,
        description="Optional list of Purview entities to rename or re-describe.",
    )

    @model_validator(mode="after")
    def _validate_operations(self) -> "UtilityManifest":
        if self.registry_query is None and not self.entity_updates:
            raise ValueError("Manifest must define at least one operation: registry_query or entity_updates.")
        if self.entity_updates and not self.purview_account:
            raise ValueError("purview_account is required when entity_updates are present.")
        return self
