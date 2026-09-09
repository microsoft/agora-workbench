"""Public resolver protocol and lazy compatibility exports."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

from .protocols import ArtifactResolver

if TYPE_CHECKING:
    from agora_workbench.code_execution.data_access.artifact_resolvers import SearchIndexArtifactResolver


def __getattr__(name: str) -> object:
    if name != "SearchIndexArtifactResolver":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(
        import_module("agora_workbench.code_execution.data_access.artifact_resolvers"),
        name,
    )
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | {"SearchIndexArtifactResolver"})


__all__ = ["ArtifactResolver", "SearchIndexArtifactResolver"]
