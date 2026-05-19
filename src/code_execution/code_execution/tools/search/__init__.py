"""Tool search module."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .bm25_tool_search import BM25ToolSearchBackend
from .state_graph import StateGraph
from .state_graph_tools import (
    PlanWorkflowInput,
    LoadSkillInput,
    create_plan_workflow_descriptor,
    create_load_skill_descriptor,
)

if TYPE_CHECKING:
    from utilities.tool_search import ToolInfo, ToolSearchBackend

__all__ = [
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
) -> ToolSearchBackend:
    """Instantiate a tool search backend by type identifier.

    Args:
        backend_type: Backend selection key (currently only ``"bm25"``).
        tools: Tool metadata to index.

    Returns:
        A ready-to-use :class:`~utilities.tool_search.ToolSearchBackend`.

    Raises:
        ValueError: If *backend_type* is not recognized.
    """
    if backend_type == "bm25":
        from .bm25_tool_search import BM25ToolSearchBackend

        return BM25ToolSearchBackend(tools=tools)
    else:
        raise ValueError(f"Unknown tool search backend: {backend_type!r}. Available: 'bm25'")
