"""
FunctionTool wrappers for the PlanStore.

This module is the only part of the planning package that depends on
agent_framework.  The rest of the package (store.py, models.py, tools.py) is
framework-agnostic.

Delegates to :mod:`planning.tools` for the core async logic and
:class:`~code_execution.tools.tool_descriptor.ToolDescriptor` objects, then wraps each
descriptor in a ``FunctionTool``.

Tool factory presets
--------------------
- ``create_plan_tools(store)``      — full read/write (planning + execution)
- ``create_read_only_tools(store)`` — view + query only (presentation stage)
- ``create_execution_tools(store)`` — status updates + view, no structural changes
"""

from __future__ import annotations

try:
    from agent_framework import FunctionTool
except ImportError as e:
    raise ImportError(
        "agent-framework is required for MAF adapters. Install with: pip install agora-workbench[maf]"
    ) from e

from ..store import PlanStore
from ..tools import (
    # Input models (re-exported so existing imports keep working)
    create_execution_descriptors,
    create_plan_descriptors,
    create_read_only_descriptors,
)


def _to_function_tool(descriptor) -> FunctionTool:
    """Convert a :class:`~code_execution.tools.tool_descriptor.ToolDescriptor` to a ``FunctionTool``."""
    return FunctionTool(
        name=descriptor.name,
        description=descriptor.description,
        func=descriptor.func,
        input_model=descriptor.input_model,
        approval_mode="never_require",
    )


# ── Factory functions ─────────────────────────────────────────────────────────


def create_plan_tools(store: PlanStore) -> list[FunctionTool]:
    """Create the full set of read/write plan tools bound to *store*."""
    return [_to_function_tool(d) for d in create_plan_descriptors(store)]


def create_read_only_tools(store: PlanStore) -> list[FunctionTool]:
    """Create read-only tools: view_plan, query_steps, plan_summary, get_history."""
    return [_to_function_tool(d) for d in create_read_only_descriptors(store)]


def create_execution_tools(store: PlanStore) -> list[FunctionTool]:
    """Create execution-stage tools: view, query, status updates."""
    return [_to_function_tool(d) for d in create_execution_descriptors(store)]
