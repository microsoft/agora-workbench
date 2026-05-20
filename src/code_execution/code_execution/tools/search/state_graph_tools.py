"""
Framework-agnostic descriptor factories for workflow planning and skill-loader tools.

Provides:

* :func:`create_plan_workflow_descriptor` — builds the
  ``plan_{name}_workflow`` :class:`~code_execution.tools.tool_descriptor.ToolDescriptor`.
* :func:`create_load_skill_descriptor` — builds the ``load_{name}_skill``
  :class:`~code_execution.tools.tool_descriptor.ToolDescriptor`.

No agent-framework imports.  These tools are registered server-side by
each MCP server's :meth:`~code_execution.server.CodeExecutionServer._setup_workflow_planning_tools`.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from utilities.tool_search import ToolInfo
from .state_graph import StateGraph, _discover_skills
from ..tool_descriptor import ToolDescriptor

LOGGER = logging.getLogger(__name__)


# ============================================================================
# Input models (framework-agnostic)
# ============================================================================


class PlanWorkflowInput(BaseModel):
    """Input model for the ``plan_{name}_workflow`` tool."""

    domain: str = Field(
        default="",
        description="Domain name (e.g. 'powergrid'). Empty string returns all domains.",
    )
    mode: Literal["overview", "next_steps", "path", "tool"] = Field(
        default="overview",
        description=(
            "Query mode. "
            "'overview': full graph with states, transitions, and skills. "
            "'next_steps': tools and skills reachable from the current state. "
            "'path': suggested path between two states. "
            "'tool': state transition details for a specific tool."
        ),
    )
    current_state: str = Field(
        default="",
        description="State token for 'next_steps' / 'path' modes (e.g. 'powergrid.network_loaded').",
    )
    target_state: str = Field(
        default="",
        description="Target state token for 'path' mode.",
    )
    tool_name: str = Field(
        default="",
        description="Tool name for 'tool' mode.",
    )


class LoadSkillInput(BaseModel):
    """Input model for the ``load_skill`` tool."""

    skill_name: str = Field(
        description=(
            "Name of the skill to load (e.g. 'flowsheet-setup', "
            "'grid-converter').  Use plan_{name}_workflow to "
            "discover available skill names."
        ),
    )


# ============================================================================
# Framework-agnostic factories
# ============================================================================


def create_plan_workflow_descriptor(
    server_name: str,
    tools: list[ToolInfo] | None = None,
    domains_dir: Path | None = None,
    extra_skill_dirs: list[Path] | None = None,
) -> ToolDescriptor:
    """Create a ``plan_{name}_workflow`` :class:`~code_execution.tools.tool_descriptor.ToolDescriptor`.

    Parameters
    ----------
    server_name : str
        MCP server / domain name (e.g. ``"powergrid"``).  Used to generate
        the tool name ``plan_{server_name}_workflow``.
    tools : list[ToolInfo] | None
        Tool metadata to index in the state graph.  Defaults to an empty
        list (no state-annotated tools).  When used server-side, pass the
        server's own tool catalog converted to :class:`~utilities.tool_search.ToolInfo`.
    domains_dir : Path | None
        Root of the ``domains/`` directory tree.  When None, skill and
        state discovery is skipped (only tool state annotations are used).
    extra_skill_dirs : list[Path] | None
        Additional top-level skill directories (e.g. ``planning/skills``).

    Returns
    -------
    ToolDescriptor
        Named ``plan_{server_name}_workflow``.
    """
    if tools is None:
        tools = []
    graph = StateGraph(tools, domains_dir, extra_skill_dirs)

    async def plan_workflow(
        domain: str = "",
        mode: str = "overview",
        current_state: str = "",
        target_state: str = "",
        tool_name: str = "",
    ) -> str:
        """Plan and navigate domain workflow states.

        Use this tool to understand workflow structure, plan sequences of
        tool calls, and discover skills that cover common workflows.

        Args:
            domain: Domain name (e.g. 'powergrid'). Empty for all domains.
            mode: Query mode — 'overview', 'next_steps', 'path', or 'tool'.
            current_state: State token for 'next_steps' / 'path' modes.
            target_state: Target state for 'path' mode.
            tool_name: Tool name for 'tool' mode.
        """
        try:
            if mode == "overview":
                result = graph.overview(domain)
            elif mode == "next_steps":
                result = graph.from_state(current_state)
            elif mode == "path":
                result = graph.path(current_state, target_state)
            elif mode == "tool":
                result = graph.tool_lookup(tool_name)
            else:
                result = {"error": f"Unknown mode '{mode}'. Use: overview, next_steps, path, tool."}
            return json.dumps(result)
        except Exception as exc:
            LOGGER.error("plan_%s_workflow failed: %s", server_name, exc, exc_info=True)
            return json.dumps({"error": f"{type(exc).__name__}: {exc}"})

    return ToolDescriptor(
        name=f"plan_{server_name}_workflow",
        description=(
            f"Plan and navigate {server_name} workflow states.  Use 'overview' "
            f"mode at the start of a task to see the full workflow map, "
            f"'next_steps' to explore what's possible from your current state, "
            f"and 'path' to plan a route between two workflow states.  "
            f"The state graph describes well-known paths — for tasks not "
            f"covered, use execute_{server_name}_code directly."
        ),
        input_model=PlanWorkflowInput,
        func=plan_workflow,
    )


def create_load_skill_descriptor(
    server_name: str,
    domains_dir: Path | None = None,
    extra_skill_dirs: list[Path] | None = None,
) -> ToolDescriptor:
    """Create a ``load_{name}_skill`` :class:`~code_execution.tools.tool_descriptor.ToolDescriptor`.

    The tool reads the full SKILL.md content for a named skill and returns
    it so the agent can follow the skill's instructions.  Skills are
    discovered from ``domains/*/skills/`` and any *extra_skill_dirs*.

    Parameters
    ----------
    server_name : str
        MCP server / domain name (e.g. ``"powergrid"``).  Used to generate
        the tool name ``load_{server_name}_skill``.
    domains_dir : Path | None
        Root of the ``domains/`` directory tree.  When None, no skills
        are discovered (the tool will report no skills available).
    extra_skill_dirs : list[Path] | None
        Additional top-level skill directories (e.g. ``planning/skills``).

    Returns
    -------
    ToolDescriptor
        Named ``load_{server_name}_skill``.
    """
    _index: dict[str, str] | None = None

    def _build_index() -> dict[str, str]:
        """Build a name → absolute-path index of all discovered skills."""
        nonlocal _index
        if _index is None:
            skills = _discover_skills(domains_dir, extra_skill_dirs)
            _index = {s["name"]: s["abs_path"] for s in skills}
            LOGGER.info("load_%s_skill index built with %d skills", server_name, len(_index))
        return _index

    async def load_skill(skill_name: str) -> str:
        """Load the full content of a skill by name.

        Returns the SKILL.md markdown body so you can follow its
        instructions.  Use ``plan_{name}_workflow`` first to discover
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
            LOGGER.error("load_%s_skill failed to read %s: %s", server_name, abs_path, exc)
            return json.dumps({"error": f"Failed to read skill file: {exc}"})

    return ToolDescriptor(
        name=f"load_{server_name}_skill",
        description=(
            f"Load the full content of a {server_name} skill by name.  Skills "
            f"contain step-by-step instructions, best practices, and sub-skill "
            f"references for domain workflows.  Use plan_{server_name}_workflow "
            f"to discover available skill names, then call load_{server_name}_skill "
            f"to get the detailed instructions before starting a workflow."
        ),
        input_model=LoadSkillInput,
        func=load_skill,
    )
