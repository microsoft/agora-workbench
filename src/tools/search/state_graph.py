"""
State graph for workflow-oriented tool and skill discovery.

The graph is built from two sources:

1. Tool state transitions supplied via :class:`~utilities.tool_search.ToolInfo` metadata
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

from utilities.tool_search import ToolInfo, ToolSearchBackend, ToolSearchResult

LOGGER = logging.getLogger(__name__)


# Path to the domains/ directory (may not exist if domains have been removed).
_DOMAINS_DIR = Path(__file__).resolve().parent.parent.parent / "domains"


# ============================================================================
# Input model
# ============================================================================


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
# StateGraph as a ToolSearchBackend
# ============================================================================


class StateGraphToolSearchBackend(ToolSearchBackend):
    """A :class:`~utilities.tool_search.ToolSearchBackend` backed by a :class:`StateGraph`.

    Searches the state graph using keyword matching against tool names,
    descriptions, required states, and produced states.  Results include
    state-transition metadata that helps the agent plan multi-step workflows.

    This backend is designed for server-side use alongside
    :class:`~tools.search.bm25_tool_search.BM25ToolSearchBackend`.  When
    registered together they provide complementary views of the tool catalog:
    BM25 gives fast text-similarity search while this backend surfaces
    workflow-oriented structure.

    Args:
        graph: A pre-built :class:`StateGraph` over the server's domain tools.
    """

    def __init__(self, graph: StateGraph) -> None:
        super().__init__()
        self._graph = graph

    async def search(self, query: str, top: int = 5) -> list[ToolSearchResult]:
        """Search the state graph for tools relevant to *query*.

        Scores each state-annotated tool by counting how many tokens from
        *query* appear in the concatenated text of its name, description,
        required states, and produced states.  Returns the top-*top* hits
        in descending score order.

        Args:
            query: Natural-language description or tool name.
            top: Maximum number of results to return.

        Returns:
            List of :class:`~utilities.tool_search.ToolSearchResult` ordered
            by descending relevance.  May include fewer than *top* results if
            the graph has few state-annotated tools.
        """
        if not query:
            return []

        query_tokens = set(re.findall(r"[a-z0-9_]+", query.lower()))
        if not query_tokens:
            return []

        scored: list[tuple[ToolInfo, float]] = []
        for tool in self._graph._tools:
            text = (
                f"{tool.name} {tool.description} "
                f"{' '.join(tool.state_requires)} {' '.join(tool.state_produces)} "
                f"{' '.join(tool.affordances)}"
            )
            tool_tokens = set(re.findall(r"[a-z0-9_]+", text.lower()))
            score = float(len(query_tokens & tool_tokens))
            if score > 0:
                scored.append((tool, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        results: list[ToolSearchResult] = []
        for tool, score in scored[:top]:
            results.append(
                ToolSearchResult(
                    name=tool.name,
                    server_name=tool.server_name,
                    description=tool.description,
                    execution_type="mcp",
                    score=score,
                    state_requires=list(tool.state_requires),
                    state_produces=list(tool.state_produces),
                )
            )
        return results

    def overview_json(self, domain: str = "") -> str:
        """Return the full state-graph overview as a JSON string.

        This is equivalent to calling ``query_state_graph`` with
        ``mode='overview'`` and subsumes the old ``list_{name}_domain_tools``
        meta-tool when ``top=999`` is used on the search tool.

        Args:
            domain: Filter to a specific domain, or empty for all domains.

        Returns:
            JSON string of the overview dict.
        """
        return json.dumps(self._graph.overview(domain))
