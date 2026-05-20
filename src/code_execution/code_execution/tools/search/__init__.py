"""Tool search module."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .azure_ai_tool_search import AzureAIToolSearchBackend
from .bm25_tool_search import BM25ToolSearchBackend
from .state_graph import StateGraph
from .state_graph_tools import (
    PlanWorkflowInput,
    LoadSkillInput,
    create_plan_workflow_descriptor,
    create_load_skill_descriptor,
)

if TYPE_CHECKING:
    from code_execution.tools.tool_search import ToolInfo, ToolSearchBackend

__all__ = [
    "AzureAIToolSearchBackend",
    "BM25ToolSearchBackend",
    "StateGraph",
    # Framework-agnostic descriptor factories and input models
    "PlanWorkflowInput",
    "LoadSkillInput",
    "create_plan_workflow_descriptor",
    "create_load_skill_descriptor",
    # Factory
    "create_tool_search_backend",
]


def create_tool_search_backend(
    backend_type: str,
    tools: list[ToolInfo],
    **kwargs,
) -> ToolSearchBackend:
    """Instantiate a tool search backend by type identifier.

    Args:
        backend_type: Backend selection key (``"bm25"`` or ``"azure_ai_search"``).
        tools: Tool metadata to index.
        **kwargs: Backend-specific keyword arguments.  For ``"bm25"``, accepts
            ``skills`` (list of skill metadata dicts) and ``server_name`` (str).

    Returns:
        A ready-to-use :class:`~code_execution.tools.tool_search.ToolSearchBackend`.

    Raises:
        ValueError: If *backend_type* is not recognized.
    """
    if backend_type == "bm25":
        from .bm25_tool_search import BM25ToolSearchBackend

        return BM25ToolSearchBackend(
            tools=tools,
            skills=kwargs.get("skills"),
            server_name=kwargs.get("server_name", ""),
        )
    elif backend_type == "azure_ai_search":
        from .azure_ai_tool_search import AzureAIToolSearchBackend

        return AzureAIToolSearchBackend(tools=tools, **kwargs)
    else:
        raise ValueError(f"Unknown tool search backend: {backend_type!r}. Available: 'bm25', 'azure_ai_search'")
