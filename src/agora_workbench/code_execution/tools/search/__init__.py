"""Tool search exports with an optional Azure AI Search backend."""

from __future__ import annotations

from importlib import import_module

from agora_workbench.code_execution.tools.tool_search import ToolSearchBackend

from .bm25_tool_search import BM25ToolSearchBackend
from .state_graph import StateGraph
from .state_graph_tools import (
    LoadSkillInput,
    PlanWorkflowInput,
    create_load_skill_descriptor,
    create_plan_workflow_descriptor,
)

_AZURE_AVAILABLE = False

try:
    from .azure_ai_tool_search import AzureAIToolSearchBackend
except ModuleNotFoundError as exc:
    if exc.name != "azure" and not (exc.name or "").startswith("azure."):
        raise
else:
    _AZURE_AVAILABLE = True


def _missing_azure_extra() -> ImportError:
    return ImportError("Azure AI tool search requires the 'agora-workbench[azure]>=0.3.0' extra.")


def __getattr__(name: str) -> object:
    if name == "azure_ai_tool_search":
        if not _AZURE_AVAILABLE:
            raise _missing_azure_extra()
        value = import_module(f"{__name__}.{name}")
        globals()[name] = value
        return value
    if name == "AzureAIToolSearchBackend":
        if not _AZURE_AVAILABLE:
            raise _missing_azure_extra()
        value = getattr(import_module(f"{__name__}.azure_ai_tool_search"), name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | {"AzureAIToolSearchBackend", "azure_ai_tool_search"})


__all__ = [
    "BM25ToolSearchBackend",
    "StateGraph",
    "PlanWorkflowInput",
    "LoadSkillInput",
    "create_plan_workflow_descriptor",
    "create_load_skill_descriptor",
    "create_tool_search_backend",
]

if _AZURE_AVAILABLE:
    __all__.append("AzureAIToolSearchBackend")


def create_tool_search_backend(
    backend_type: str,
    **kwargs,
) -> ToolSearchBackend:
    """Instantiate a tool search backend by type identifier."""
    if backend_type == "bm25":
        return BM25ToolSearchBackend()
    if backend_type == "azure_ai_search":
        if not _AZURE_AVAILABLE:
            raise RuntimeError(str(_missing_azure_extra()))
        return AzureAIToolSearchBackend(
            index_name=kwargs.get("index_name"),
            endpoint=kwargs.get("endpoint"),
        )
    raise ValueError(f"Unknown tool search backend: {backend_type!r}. Available: 'bm25', 'azure_ai_search'")
