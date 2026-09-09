"""Code execution package exports."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .code_execution_models import AssetSpec, CodeExecutionResult, ServerConfig, SidecarConfig
    from .data_access.publishers import ServerPublisher
    from .server import CodeExecutionServer
    from .skills import Skill, discover_skills
    from .tool_registry import (
        ReturnSpec,
        State,
        StateTransition,
        ToolDefinition,
        ToolParameter,
        ToolRegistry,
    )

_LAZY_EXPORTS = {
    "AssetSpec": ("agora_workbench.code_execution.code_execution_models", "AssetSpec"),
    "CodeExecutionResult": ("agora_workbench.code_execution.code_execution_models", "CodeExecutionResult"),
    "CodeExecutionServer": ("agora_workbench.code_execution.server", "CodeExecutionServer"),
    "ReturnSpec": ("agora_workbench.code_execution.tool_registry", "ReturnSpec"),
    "ServerConfig": ("agora_workbench.code_execution.code_execution_models", "ServerConfig"),
    "ServerPublisher": ("agora_workbench.code_execution.data_access.publishers", "ServerPublisher"),
    "SidecarConfig": ("agora_workbench.code_execution.code_execution_models", "SidecarConfig"),
    "Skill": ("agora_workbench.code_execution.skills", "Skill"),
    "State": ("agora_workbench.code_execution.tool_registry", "State"),
    "StateTransition": ("agora_workbench.code_execution.tool_registry", "StateTransition"),
    "ToolDefinition": ("agora_workbench.code_execution.tool_registry", "ToolDefinition"),
    "ToolParameter": ("agora_workbench.code_execution.tool_registry", "ToolParameter"),
    "ToolRegistry": ("agora_workbench.code_execution.tool_registry", "ToolRegistry"),
    "discover_skills": ("agora_workbench.code_execution.skills", "discover_skills"),
}
_LAZY_SUBMODULES = {
    "agent_guidance",
    "auth",
    "code_execution_models",
    "data_access",
    "sessions",
    "skills",
    "tools",
}


def __getattr__(name: str) -> object:
    if name in _LAZY_SUBMODULES:
        value = import_module(f"{__name__}.{name}")
        globals()[name] = value
        return value
    try:
        module_name, attribute_name = _LAZY_EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | _LAZY_EXPORTS.keys() | _LAZY_SUBMODULES)


__all__ = [
    "AssetSpec",
    "CodeExecutionResult",
    "CodeExecutionServer",
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
