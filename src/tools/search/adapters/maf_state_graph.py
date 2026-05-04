"""MAF FunctionTool factories for the state graph and skill loader.

Requires the ``maf`` extra: ``pip install agora-workbench[maf]``
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import BaseModel, Field

try:
    from agent_framework import FunctionTool
except ImportError as e:
    raise ImportError(
        "agent-framework is required for MAF adapters. "
        "Install with: pip install agora-workbench[maf]"
    ) from e

from tools.search.build_tool_list import ToolInfo
from tools.search.state_graph import StateGraph, _discover_skills, _DOMAINS_DIR

LOGGER = logging.getLogger(__name__)


class QueryStateGraphInput(BaseModel):
    """Input model for the ``query_state_graph`` FunctionTool."""

    domain: str = Field(
        default="",
        description="Domain name (e.g. 'powergrid'). Empty string returns all domains.",
    )
    mode: str = Field(
        default="overview",
        description=(
            "Query mode. "
            "'overview': full graph with states, transitions, and skills. "
            "'from_state': tools and skills reachable from a given state. "
            "'path': suggested path between two states. "
            "'tool': state transition details for a specific tool."
        ),
    )
    state: str = Field(
        default="",
        description="State token for 'from_state' mode (e.g. 'powergrid.network_loaded').",
    )
    target_state: str = Field(
        default="",
        description="Target state token for 'path' mode.",
    )
    tool_name: str = Field(
        default="",
        description="Tool name for 'tool' mode.",
    )

# ============================================================================
# FunctionTool factory
# ============================================================================


def create_query_state_graph_function(
    tools: list[ToolInfo] | None = None,
    domains_dir: Path = _DOMAINS_DIR,
    extra_skill_dirs: list[Path] | None = None,
) -> FunctionTool:
    """Create a ``query_state_graph`` :class:`FunctionTool`.

    Parameters
    ----------
    tools : list[ToolInfo] | None
        Tool metadata (typically from :func:`build_tool_list`).
        If ``None``, the graph will lazily discover tools from MCP
        servers on first query so that domain meta-tools are available.
    domains_dir : Path
        Root of the ``domains/`` directory tree.
    extra_skill_dirs : list[Path] | None
        Additional top-level skill directories (e.g. ``planning/skills``).

    Returns
    -------
    FunctionTool
        Named ``query_state_graph``.
    """
    _graph_holder: dict[str, StateGraph | None] = {"graph": None}

    if tools is not None:
        _graph_holder["graph"] = StateGraph(tools, domains_dir, extra_skill_dirs)

    async def _ensure_graph() -> StateGraph:
        if _graph_holder["graph"] is None:
            from tools.search.build_tool_list import build_tool_list

            discovered = await build_tool_list()
            _graph_holder["graph"] = StateGraph(discovered, domains_dir, extra_skill_dirs)
            LOGGER.info(
                "StateGraph lazily built with %d state-annotated tools",
                len([t for t in discovered if t.state_requires or t.state_produces]),
            )
        return _graph_holder["graph"]

    async def query_state_graph(
        domain: str = "",
        mode: str = "overview",
        state: str = "",
        target_state: str = "",
        tool_name: str = "",
    ) -> str:
        """Query the domain workflow state graph.

        Use this tool to understand workflow structure, plan sequences of
        tool calls, and discover skills that cover common workflows.

        Args:
            domain: Domain name (e.g. 'powergrid'). Empty for all domains.
            mode: Query mode — 'overview', 'from_state', 'path', or 'tool'.
            state: State token for 'from_state' / 'path' modes.
            target_state: Target state for 'path' mode.
            tool_name: Tool name for 'tool' mode.
        """
        try:
            graph = await _ensure_graph()
            if mode == "overview":
                result = graph.overview(domain)
            elif mode == "from_state":
                result = graph.from_state(state)
            elif mode == "path":
                result = graph.path(state, target_state)
            elif mode == "tool":
                result = graph.tool_lookup(tool_name)
            else:
                result = {"error": f"Unknown mode '{mode}'. Use: overview, from_state, path, tool."}
            return json.dumps(result)
        except Exception as exc:
            LOGGER.error("query_state_graph failed: %s", exc, exc_info=True)
            return json.dumps({"error": f"{type(exc).__name__}: {exc}"})

    return FunctionTool(
        name="query_state_graph",
        description=(
            "Query the domain workflow state graph to understand available "
            "workflow states, transitions between them, and which tools and "
            "skills are relevant at each stage.  Use 'overview' mode at the "
            "start of a task to see the full workflow map, 'from_state' to "
            "explore what's possible from your current position, and 'path' "
            "to plan a route between two workflow states.  The state graph "
            "describes well-known paths — for tasks not covered, use "
            "execute_{domain}_code directly."
        ),
        approval_mode="never_require",
        func=query_state_graph,
        input_model=QueryStateGraphInput,
    )


# ============================================================================
# load_skill FunctionTool factory
# ============================================================================


class LoadSkillInput(BaseModel):
    """Input model for the ``load_skill`` FunctionTool."""

    skill_name: str = Field(
        description=(
            "Name of the skill to load (e.g. 'flowsheet-setup', "
            "'grid-converter').  Use query_state_graph to "
            "discover available skill names."
        ),
    )


def create_load_skill_function(
    domains_dir: Path = _DOMAINS_DIR,
    extra_skill_dirs: list[Path] | None = None,
) -> FunctionTool:
    """Create a ``load_skill`` :class:`FunctionTool`.

    The tool reads the full SKILL.md content for a named skill and returns
    it so the agent can follow the skill's instructions.  Skills are
    discovered from ``domains/*/skills/`` and any *extra_skill_dirs*.

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
    _index: dict[str, str] | None = None

    def _build_index() -> dict[str, str]:
        """Build a name → absolute-path index of all discovered skills."""
        nonlocal _index
        if _index is None:
            skills = _discover_skills(domains_dir, extra_skill_dirs)
            _index = {s["name"]: s["abs_path"] for s in skills}
            LOGGER.info("load_skill index built with %d skills", len(_index))
        return _index

    async def load_skill(skill_name: str) -> str:
        """Load the full content of a skill by name.

        Returns the SKILL.md markdown body so you can follow its
        instructions.  Use ``query_state_graph`` first to discover
        which skill to load.

        Args:
            skill_name: Exact skill name (e.g. 'flowsheet-setup').
        """
        index = _build_index()
        abs_path = index.get(skill_name)
        if abs_path is None:
            available = sorted(index.keys())
            return json.dumps(
                {
                    "error": f"Skill '{skill_name}' not found.",
                    "available_skills": available,
                }
            )
        try:
            content = Path(abs_path).read_text(encoding="utf-8")
            return content
        except OSError as exc:
            LOGGER.error("load_skill failed to read %s: %s", abs_path, exc)
            return json.dumps({"error": f"Failed to read skill file: {exc}"})

    return FunctionTool(
        name="load_skill",
        description=(
            "Load the full content of a skill by name.  Skills contain "
            "step-by-step instructions, best practices, and sub-skill "
            "references for domain workflows.  Use query_state_graph to "
            "discover available skill names, then call load_skill to get "
            "the detailed instructions before starting a workflow."
        ),
        approval_mode="never_require",
        func=load_skill,
        input_model=LoadSkillInput,
    )
