"""Agora Workbench — A toolkit for building MCP servers with sandboxed Python execution."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agora_workbench.base import BaseMCPServer
    from agora_workbench.code_execution import (
        AssetSpec,
        CodeExecutionResult,
        CodeExecutionServer,
        ReturnSpec,
        ServerConfig,
        SidecarConfig,
        Skill,
        State,
        StateTransition,
        ToolDefinition,
        ToolParameter,
        ToolRegistry,
        discover_skills,
    )
    from agora_workbench.connector import (
        ConnectorServer,
        DispatcherConfig,
        DispatcherServer,
        GatewayConfig,
        GatewayPolicy,
        GatewayServer,
        RouterConfig,
        RouterServer,
        UpstreamConfig,
        WorkerConfig,
    )

_CODE_EXECUTION_EXPORTS = {
    "AssetSpec",
    "CodeExecutionResult",
    "CodeExecutionServer",
    "ReturnSpec",
    "ServerConfig",
    "SidecarConfig",
    "Skill",
    "State",
    "StateTransition",
    "ToolDefinition",
    "ToolParameter",
    "ToolRegistry",
    "discover_skills",
}
_CONNECTOR_EXPORTS = {
    "ConnectorServer",
    "DispatcherConfig",
    "DispatcherServer",
    "GatewayConfig",
    "GatewayPolicy",
    "GatewayServer",
    "RouterConfig",
    "RouterServer",
    "UpstreamConfig",
    "WorkerConfig",
}
_LAZY_SUBMODULES = {
    "base",
    "code_execution",
    "connector",
    "data_lake",
    "deployment",
    "skills",
}


def __getattr__(name: str) -> object:
    if name in _LAZY_SUBMODULES:
        value = import_module(f"{__name__}.{name}")
        globals()[name] = value
        return value
    if name == "BaseMCPServer":
        module_name = "agora_workbench.base"
    elif name in _CODE_EXECUTION_EXPORTS:
        module_name = "agora_workbench.code_execution"
    elif name in _CONNECTOR_EXPORTS:
        module_name = "agora_workbench.connector"
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | _LAZY_SUBMODULES | _CODE_EXECUTION_EXPORTS | _CONNECTOR_EXPORTS | {"BaseMCPServer"})


__all__ = [
    "AssetSpec",
    "BaseMCPServer",
    "CodeExecutionResult",
    "CodeExecutionServer",
    "ConnectorServer",
    "DispatcherConfig",
    "DispatcherServer",
    "GatewayConfig",
    "GatewayPolicy",
    "GatewayServer",
    "ReturnSpec",
    "RouterConfig",
    "RouterServer",
    "ServerConfig",
    "Skill",
    "SidecarConfig",
    "State",
    "StateTransition",
    "ToolDefinition",
    "ToolParameter",
    "ToolRegistry",
    "UpstreamConfig",
    "WorkerConfig",
    "discover_skills",
]
