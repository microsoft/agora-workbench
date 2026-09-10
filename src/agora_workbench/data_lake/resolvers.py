"""Public resolver protocol and compatibility exports."""

from agora_workbench.code_execution.data_access.artifact_resolvers import SearchIndexArtifactResolver
from .protocols import ArtifactResolver

__all__ = ["ArtifactResolver", "SearchIndexArtifactResolver"]
