"""
Pydantic models for the artifact registry index.

This file is AUTO-GENERATED from index.jinja by generate_models.py.
Do not edit manually - regenerate using: python generate_models.py
Generated: 2026-01-28T15:16:02.434940
"""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, field_serializer


class ArtifactRegistryDocument(BaseModel):
    """
    Document model for the artifact-registry-v1 index.

    Represents an artifact with enriched metadata from both blob-details
    and semantic dataset registry.
    """

    # Key field - required
    artifact_id: str = Field(..., description="Artifact Id")

    # Document fields
    artifact_type: Optional[str] = Field(None, description="Artifact Type")
    name: Optional[str] = Field(None, description="Name")
    description: Optional[str] = Field(None, description="Description")
    description_vector: Optional[List[float]] = Field(
        None, description="Description Vector (vector with 3072 dimensions)", min_length=3072, max_length=3072
    )
    semantic_dataset_id: Optional[str] = Field(None, description="Semantic Dataset Id")
    semantic_dataset_name: Optional[str] = Field(None, description="Semantic Dataset Name")
    semantic_dataset_description: Optional[str] = Field(None, description="Semantic Dataset Description")
    semantic_dataset_description_vector: Optional[List[float]] = Field(
        None,
        description="Semantic Dataset Description Vector (vector with 3072 dimensions)",
        min_length=3072,
        max_length=3072,
    )
    domain: Optional[str] = Field(None, description="Domain")
    rbacScope: Optional[str] = Field(None, description="Rbacscope", alias="rbacScope")
    detail_index: Optional[str] = Field(None, description="Detail Index")
    detail_key: Optional[str] = Field(None, description="Detail Key")
    source: Optional[str] = Field(None, description="Source")

    # Timestamp fields
    created_at: Optional[datetime] = Field(None, description="Created At")
    updated_at: Optional[datetime] = Field(None, description="Updated At")

    @field_serializer("created_at", "updated_at")
    def serialize_datetime(self, dt: Optional[datetime]) -> Optional[str]:
        """Serialize datetime to ISO 8601 format with Z suffix for Azure Search."""
        if dt is None:
            return None
        # Ensure UTC and format with Z suffix
        if dt.tzinfo is None:
            return dt.isoformat() + "Z"
        return dt.isoformat().replace("+00:00", "Z")

    class Config:
        """Pydantic configuration."""

        populate_by_name = True
