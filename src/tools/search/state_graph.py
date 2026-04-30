"""
State graph query tool for workflow-oriented tool and skill discovery.

Provides :func:`create_query_state_graph_function`, which builds a
``query_state_graph`` :class:`~agent_framework.FunctionTool`.  The tool
lets the agent explore domain workflow graphs — states, transitions,
skills, and reachability — so it can reason about *what needs to happen*
rather than *which API to call*.

The graph is built from three sources:

1. Tool state transitions (via :func:`build_tool_list`)
2. Skill state annotations (``states`` field in SKILL.md frontmatter)
"""

from __future__ import annotations

import importlib
import json
import logging
import re
from collections import defaultdict
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import yaml
from agent_framework import FunctionTool
from pydantic import BaseModel, Field

from tools.search.build_tool_list import ToolInfo

LOGGER = logging.getLogger(__name__)


# Path to the domains/ directory (may not exist if domains have been removed).
_DOMAINS_DIR = Path(__file__).resolve().parent.parent.parent / "domains"


# ============================================================================
# Input model
# ============================================================================


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
# Skill metadata
# ============================================================================


def _parse_skill_frontmatter(path: Path) -> dict[str, Any]:
    """Extract YAML frontmatter from a SKILL.md file."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    match = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not match:
        return {}
    try:
        return yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        return {}


def _discover_skills(
    domains_dir: Path = _DOMAINS_DIR,
    extra_skill_dirs: list[Path] | None = None,
) -> list[dict[str, Any]]:
    """Discover all SKILL.md files under ``domains/*/skills/`` and parse frontmatter.

    Parameters
    ----------
    domains_dir : Path
        Root of the ``domains/`` directory tree.
    extra_skill_dirs : list[Path] | None
        Additional top-level directories to search for SKILL.md files
        (e.g. ``planning/skills``).  Each is searched recursively up to
        3 levels deep, like domain skill directories.

    Returns a list of dicts with keys: name, description, domain, states, path.
    """
    skills: list[dict[str, Any]] = []
    if not domains_dir.is_dir():
        return skills

    for domain_dir in sorted(domains_dir.iterdir()):
        if not domain_dir.is_dir():
            continue
        skills_root = domain_dir / "skills"
        if not skills_root.is_dir():
            continue
        domain_name = domain_dir.name

        # Search up to 3 levels deep for SKILL.md
        for skill_md in sorted(skills_root.rglob("SKILL.md")):
            fm = _parse_skill_frontmatter(skill_md)
            if not fm.get("name"):
                continue
            skills.append(
                {
                    "name": fm["name"],
                    "description": fm.get("description", ""),
                    "domain": domain_name,
                    "states": fm.get("states", []),
                    "path": str(skill_md.relative_to(domains_dir)),
                    "abs_path": str(skill_md),
                }
            )

    for extra_dir in extra_skill_dirs or []:
        if not extra_dir.is_dir():
            continue
        pkg_name = extra_dir.parent.name  # e.g. "planning"
        for skill_md in sorted(extra_dir.rglob("SKILL.md")):
            fm = _parse_skill_frontmatter(skill_md)
            if not fm.get("name"):
                continue
            skills.append(
                {
                    "name": fm["name"],
                    "description": fm.get("description", ""),
                    "domain": pkg_name,
                    "states": fm.get("states", []),
                    "path": str(skill_md.relative_to(extra_dir.parent)),
                    "abs_path": str(skill_md),
                }
            )
    return skills


# ============================================================================
# State graph
# ============================================================================


class StateGraph:
    """Queryable state graph built from domain states, tool transitions, and skills.

    Parameters
    ----------
    tools : list[ToolInfo]
        Tool metadata including ``state_requires`` / ``state_produces``.
    domains_dir : Path, optional
        Root of the ``domains/`` directory tree.  Defaults to the
        standard location relative to this file.
    """

    def __init__(
        self,
        tools: list[ToolInfo],
        domains_dir: Path = _DOMAINS_DIR,
        extra_skill_dirs: list[Path] | None = None,
    ) -> None:
        self._domains_dir = domains_dir
        self._extra_skill_dirs = extra_skill_dirs

        # {domain_name: {enum_member.value: enum_member.name, ...}}
        self._domain_states: dict[str, dict[str, str]] = {}
        # {domain_name: {state_token: [phrase, ...]}}
        self._domain_affordances: dict[str, dict[str, list[str]]] = {}
        self._load_domain_states()

        # Tool index: tools with state annotations
        self._tools = [t for t in tools if t.state_requires or t.state_produces]
        # {state_token: [ToolInfo, ...]}  — tools reachable *from* a state
        self._from_state: dict[str, list[ToolInfo]] = defaultdict(list)
        # {state_token: [ToolInfo, ...]}  — tools that *produce* a state
        self._to_state: dict[str, list[ToolInfo]] = defaultdict(list)
        self._build_adjacency()

        # Skills with state annotations
        self._skills = _discover_skills(domains_dir, extra_skill_dirs)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load_domain_states(self) -> None:
        """Import ``states.py`` from each domain directory."""
        if not self._domains_dir.is_dir():
            return
        for domain_dir in sorted(self._domains_dir.iterdir()):
            if not domain_dir.is_dir() or not (domain_dir / "states.py").exists():
                continue
            domain_name = domain_dir.name
            try:
                mod = importlib.import_module(f"domains.{domain_name}.states")
            except ImportError:
                continue

            # Find the state enum
            for attr_name in dir(mod):
                obj = getattr(mod, attr_name)
                if isinstance(obj, type) and issubclass(obj, Enum) and obj is not Enum:
                    self._domain_states[domain_name] = {member.value: member.name for member in obj}
                    break

            # Affordances
            raw_aff = getattr(mod, "STATE_AFFORDANCES", {})
            if raw_aff:
                self._domain_affordances[domain_name] = {
                    enum_val.value: phrases for enum_val, phrases in raw_aff.items()
                }

    def _build_adjacency(self) -> None:
        for tool in self._tools:
            for st in tool.state_requires:
                self._from_state[st].append(tool)
            for st in tool.state_produces:
                self._to_state[st].append(tool)

    def _domain_for_state(self, state_token: str) -> str:
        """Extract domain prefix from a state token like 'powergrid.network_loaded'."""
        return state_token.split(".")[0] if "." in state_token else ""

    def _tool_summary(self, tool: ToolInfo) -> dict[str, Any]:
        return {
            "name": tool.name,
            "server": tool.server_name,
            "description": tool.description,
            "requires": list(tool.state_requires),
            "produces": list(tool.state_produces),
        }

    def _skills_for_states(self, state_tokens: set[str]) -> list[dict[str, Any]]:
        """Return skills whose state range overlaps *state_tokens*."""
        matches = []
        for skill in self._skills:
            skill_states = set(skill.get("states", []))
            if skill_states and skill_states & state_tokens:
                matches.append(
                    {
                        "name": skill["name"],
                        "description": skill["description"],
                        "domain": skill["domain"],
                        "states": skill["states"],
                    }
                )
        return matches

    def _find_path(self, source: str, target: str) -> Optional[list[dict[str, Any]]]:
        """BFS to find a shortest tool-transition path from *source* to *target*.

        Returns a list of ``{"tool": ..., "from_state": ..., "to_state": ...}``
        steps, or ``None`` if no path exists.
        """
        if source == target:
            return []

        # Each node is a state token.  Edges are tools whose requires
        # includes the current state and whose produces includes the next.
        from collections import deque

        visited: set[str] = {source}
        # queue entries: (current_state, path_so_far)
        queue: deque[tuple[str, list[dict[str, Any]]]] = deque()
        queue.append((source, []))

        while queue:
            current, path = queue.popleft()
            for tool in self._from_state.get(current, []):
                for produced in tool.state_produces:
                    if produced in visited:
                        continue
                    step = {
                        "tool": tool.name,
                        "server": tool.server_name,
                        "from_state": current,
                        "to_state": produced,
                    }
                    new_path = path + [step]
                    if produced == target:
                        return new_path
                    visited.add(produced)
                    queue.append((produced, new_path))
        return None

    # ------------------------------------------------------------------
    # Public query API
    # ------------------------------------------------------------------

    def overview(self, domain: str = "") -> dict[str, Any]:
        """Full graph overview for one or all domains."""
        domains_to_show = [domain] if domain else sorted(self._domain_states.keys())

        result: list[dict[str, Any]] = []
        for d in domains_to_show:
            states = self._domain_states.get(d, {})
            if not states:
                continue

            # Build edges from tools
            edges: list[dict[str, Any]] = []
            seen_edges: set[tuple[str, str, str]] = set()
            for tool in self._tools:
                if tool.server_name != d:
                    continue
                requires = tool.state_requires if tool.state_requires else ("(start)",)
                for req in requires:
                    for prod in tool.state_produces:
                        key = (req, prod, tool.name)
                        if key not in seen_edges:
                            seen_edges.add(key)
                            edges.append({"from": req, "to": prod, "tool": tool.name})

            # Collect domain skills
            domain_skills = [
                {
                    "name": s["name"],
                    "description": s["description"],
                    "states": s["states"],
                }
                for s in self._skills
                if s["domain"] == d and s.get("states")
            ]

            affordances = {token: phrases for token, phrases in self._domain_affordances.get(d, {}).items()}

            result.append(
                {
                    "domain": d,
                    "states": states,
                    "edges": edges,
                    "affordances": affordances,
                    "skills": domain_skills,
                }
            )

        if not result:
            return self._no_results_response(domain)
        return {"domains": result}

    def from_state(self, state: str) -> dict[str, Any]:
        """Tools and skills reachable from a given state."""
        if not state:
            return {"error": "Provide a 'state' parameter (e.g. 'domain.state_name')."}

        tools_from = [self._tool_summary(t) for t in self._from_state.get(state, [])]
        # Also include tools that produce this state (how to get here)
        tools_to = [self._tool_summary(t) for t in self._to_state.get(state, [])]

        # Collect all states reachable in one hop
        next_states: set[str] = set()
        for t in self._from_state.get(state, []):
            next_states.update(t.state_produces)

        # Find relevant skills
        relevant_states = {state} | next_states
        skills = self._skills_for_states(relevant_states)

        # Affordances for the queried state
        domain = self._domain_for_state(state)
        affordances = self._domain_affordances.get(domain, {}).get(state, [])

        result: dict[str, Any] = {
            "state": state,
            "affordances": affordances,
            "tools_from_here": tools_from,
            "next_states": sorted(next_states),
        }
        if tools_to:
            result["tools_that_produce_this_state"] = tools_to
        if skills:
            result["relevant_skills"] = skills

        if not tools_from:
            result["hint"] = (
                f"No annotated tools transition from '{state}'. "
                f"You can always use execute_{domain}_code to work directly "
                f"in the {domain} environment."
            )

        return result

    def path(self, source: str, target: str) -> dict[str, Any]:
        """Find a suggested path between two states."""
        if not source or not target:
            return {"error": "Provide both 'state' (source) and 'target_state' parameters."}

        steps = self._find_path(source, target)
        if steps is None:
            domain = self._domain_for_state(source)
            return {
                "source": source,
                "target": target,
                "path": None,
                "hint": (
                    f"No annotated tool path from '{source}' to '{target}'. "
                    f"You can use execute_{domain}_code to accomplish tasks "
                    f"not covered by the annotated tool graph."
                ),
            }

        # Collect all states along the path for skill matching
        path_states = {source, target}
        for step in steps:
            path_states.add(step["from_state"])
            path_states.add(step["to_state"])

        skills = self._skills_for_states(path_states)

        result: dict[str, Any] = {
            "source": source,
            "target": target,
            "path": steps,
        }
        if skills:
            result["relevant_skills"] = skills
        return result

    def tool_lookup(self, tool_name: str) -> dict[str, Any]:
        """State transition details for a specific tool."""
        if not tool_name:
            return {"error": "Provide a 'tool_name' parameter."}

        for tool in self._tools:
            if tool.name == tool_name:
                return self._tool_summary(tool)

        return {
            "error": f"Tool '{tool_name}' not found in the state graph.",
            "hint": "The tool may exist but have no state annotations. "
            "Use search_tools to find it by name or description.",
        }

    def _no_results_response(self, domain: str) -> dict[str, Any]:
        available = sorted(self._domain_states.keys())
        msg = f"No state graph found for domain '{domain}'." if domain else "No domain state graphs available."
        result: dict[str, Any] = {"message": msg}
        if available:
            result["available_domains"] = available
        result["hint"] = (
            "You can always use execute_{domain}_code to work directly "
            "in a domain environment, even without an annotated state graph."
        )
        return result


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
