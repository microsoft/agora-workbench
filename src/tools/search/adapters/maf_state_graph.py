"""MAF FunctionTool factories for the state graph and skill loader.

Wraps :func:`tools.search.state_graph_tools.create_query_state_graph_descriptor`
and :func:`tools.search.state_graph_tools.create_load_skill_descriptor` in
``FunctionTool`` objects.  This is the **only** file in the state-graph path
that imports ``agent_framework``.

Requires the ``maf`` extra: ``pip install agora-workbench[maf]``
"""

from __future__ import annotations

from pathlib import Path

try:
    from agent_framework import FunctionTool
except ImportError as e:
    raise ImportError(
        "agent-framework is required for MAF adapters. "
        "Install with: pip install agora-workbench[maf]"
    ) from e

from tools.search.build_tool_list import ToolInfo
from tools.search.state_graph import _DOMAINS_DIR
from tools.search.state_graph_tools import (
    LoadSkillInput,
    QueryStateGraphInput,
    create_load_skill_descriptor,
    create_query_state_graph_descriptor,
)

# Re-export input models so existing imports keep working
__all__ = [
    "QueryStateGraphInput",
    "LoadSkillInput",
    "create_query_state_graph_function",
    "create_load_skill_function",
]


# ============================================================================
# MAF factories
# ============================================================================


def create_query_state_graph_function(
    tools: list[ToolInfo] | None = None,
    domains_dir: Path = _DOMAINS_DIR,
    extra_skill_dirs: list[Path] | None = None,
) -> FunctionTool:
    """Create a ``query_state_graph`` :class:`FunctionTool`.

    Delegates to
    :func:`~tools.search.state_graph_tools.create_query_state_graph_descriptor`
    and wraps the result in a ``FunctionTool``.

    Parameters
    ----------
    tools : list[ToolInfo] | None
        Tool metadata (typically from :func:`build_tool_list`).
    domains_dir : Path
        Root of the ``domains/`` directory tree.
    extra_skill_dirs : list[Path] | None
        Additional top-level skill directories (e.g. ``planning/skills``).

    Returns
    -------
    FunctionTool
        Named ``query_state_graph``.
    """
    descriptor = create_query_state_graph_descriptor(tools, domains_dir, extra_skill_dirs)
    return FunctionTool(
        name=descriptor.name,
        description=descriptor.description,
        approval_mode="never_require",
        func=descriptor.func,
        input_model=QueryStateGraphInput,
    )


def create_load_skill_function(
    domains_dir: Path = _DOMAINS_DIR,
    extra_skill_dirs: list[Path] | None = None,
) -> FunctionTool:
    """Create a ``load_skill`` :class:`FunctionTool`.

    Delegates to
    :func:`~tools.search.state_graph_tools.create_load_skill_descriptor`
    and wraps the result in a ``FunctionTool``.

    Parameters
    ----------
    domains_dir : Path
        Root of the ``domains/`` directory tree.
    extra_skill_dirs : list[Path] | None
        Additional top-level skill directories (e.g. ``planning/skills``).

    Returns
    -------
    FunctionTool
        Named ``load_skill``.
    """
    descriptor = create_load_skill_descriptor(domains_dir, extra_skill_dirs)
    return FunctionTool(
        name=descriptor.name,
        description=descriptor.description,
        approval_mode="never_require",
        func=descriptor.func,
        input_model=LoadSkillInput,
    )
