"""Code execution package exports."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

from .server import CodeExecutionServer
from .code_execution_models import AssetSpec, CodeExecutionResult, ServerConfig, SidecarConfig
from .data_access.publishers import ServerPublisher
from .skills import Skill, discover_skills
from .tool_registry import (
    ReturnSpec,
    State,
    StateTransition,
    ToolDefinition,
    ToolParameter,
    ToolRegistry,
)

if TYPE_CHECKING:
    from .catalog_integration import CatalogAwareDataManager, CatalogFetcherFactory, CatalogIntegration

_CATALOG_EXPORTS = {"CatalogAwareDataManager", "CatalogFetcherFactory", "CatalogIntegration"}


def __getattr__(name: str) -> object:
    """Load catalog integration lazily to preserve data-lake import boundaries."""
    if name not in _CATALOG_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f"{__name__}.catalog_integration"), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | _CATALOG_EXPORTS)


__all__ = [
    "AssetSpec",
    "CodeExecutionResult",
    "CodeExecutionServer",
    "CatalogAwareDataManager",
    "CatalogFetcherFactory",
    "CatalogIntegration",
    "ServerConfig",
    "ServerPublisher",
    "Skill",
    "SidecarConfig",
    "discover_skills",
    "ReturnSpec",
    "State",
    "StateTransition",
    "ToolDefinition",
    "ToolParameter",
    "ToolRegistry",
]
