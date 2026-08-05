"""
State graph for workflow-oriented tool and skill discovery.

The graph is built from two sources:

1. Tool state transitions supplied via :class:`~code_execution.tools.tool_search.ToolInfo` metadata
2. Skill state annotations (``states`` field in SKILL.md frontmatter)
"""

from __future__ import annotations

import importlib
import logging
import re
from collections import defaultdict
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import yaml

from ..tool_search import ToolInfo

LOGGER = logging.getLogger(__name__)


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
    domains_dir: Path | None,
    extra_skill_dirs: list[Path] | None = None,
    domain_name: str | None = None,
) -> list[dict[str, Any]]:
    """Discover skill markdown files under ``domains/*/skills/`` and parse frontmatter.

    Any ``*.md`` file with a ``name:`` field in its YAML frontmatter is treated
    as a skill.  Files without frontmatter (READMEs, prose docs) are skipped.

    Parameters
    ----------
    domains_dir : Path | None
        Root of the ``domains/`` directory tree.  When None, domain-based
        skill discovery is skipped.
    extra_skill_dirs : list[Path] | None
        Additional top-level directories to search for skill markdown
        (e.g. ``planning/skills``).
    domain_name : str | None
        When set, restrict discovery to ``domains_dir / domain_name / skills/``
        instead of iterating every subdirectory of ``domains_dir``.  Use this
        from per-server callers so chemistry's server doesn't index
        earthscience's skills in a shared-source dev layout.

    Returns a list of dicts with keys: name, description, domain, states, path.
    """
    skills: list[dict[str, Any]] = []
    if domains_dir is None or not domains_dir.is_dir():
        return skills

    if domain_name is not None:
        candidate = domains_dir / domain_name
        domain_dirs = [candidate] if candidate.is_dir() else []
    else:
        domain_dirs = sorted(d for d in domains_dir.iterdir() if d.is_dir())

    for domain_dir in domain_dirs:
        skills_root = domain_dir / "skills"
        if not skills_root.is_dir():
            continue
        d_name = domain_dir.name

        for skill_md in sorted(skills_root.rglob("*.md")):
            fm = _parse_skill_frontmatter(skill_md)
            if not fm.get("name"):
                continue
            skills.append(
                {
                    "name": fm["name"],
                    "description": fm.get("description", ""),
                    "domain": d_name,
                    "states": fm.get("states", []),
                    "path": str(skill_md.relative_to(domains_dir)),
                    "abs_path": str(skill_md),
                }
            )

    for extra_dir in extra_skill_dirs or []:
        if not extra_dir.is_dir():
            continue
        pkg_name = extra_dir.parent.name  # e.g. "planning"
        for skill_md in sorted(extra_dir.rglob("*.md")):
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
    domains_dir : Path | None
        Root of the ``domains/`` directory tree containing domain state
        definitions and skills.  When None, skill and state discovery
        from the filesystem is skipped.
    """

    def __init__(
        self,
        tools: list[ToolInfo],
        domains_dir: Path | None = None,
        extra_skill_dirs: list[Path] | None = None,
        domain_name: str | None = None,
        skills: list[dict[str, Any]] | None = None,
        state_descriptions: dict[str, str] | None = None,
    ) -> None:
        self._domains_dir = domains_dir
        self._extra_skill_dirs = extra_skill_dirs
        self._domain_name = domain_name
        # {state_token: human-readable description} from State objects
        self._state_descriptions: dict[str, str] = state_descriptions or {}

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

        # Skills: use explicitly provided skills or discover from filesystem
        if skills is not None:
            self._skills = skills
        else:
            self._skills = _discover_skills(domains_dir, extra_skill_dirs, domain_name)

        # If no domain states were loaded from filesystem, infer a minimal
        # vocabulary from tool state annotations so overview() still works.
        if not self._domain_states and self._tools:
            self._infer_states_from_tools()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load_domain_states(self) -> None:
        """Import ``states.py`` from each domain directory."""
        if self._domains_dir is None or not self._domains_dir.is_dir():
            return
        if self._domain_name is not None:
            candidate = self._domains_dir / self._domain_name
            domain_dirs = [candidate] if candidate.is_dir() else []
        else:
            domain_dirs = sorted(d for d in self._domains_dir.iterdir() if d.is_dir())
        for domain_dir in domain_dirs:
            if not (domain_dir / "states.py").exists():
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

    def inject_bridges(self, bridges: list[dict[str, str]]) -> None:
        """Inject synthetic cross-server bridge edges into the graph.

        Each bridge is represented as a synthetic :class:`ToolInfo` entry
        with ``state_requires=(from_state,)`` and ``state_produces=(to_state,)``.
        Bridge tools are tagged with a ``bridge:`` prefix in their name and
        ``"(bridge)"`` as the server name so planner output distinguishes them
        from real tool transitions.

        Parameters
        ----------
        bridges : list[dict[str, str]]
            Each dict must have ``from_state``, ``to_state``, and optionally
            ``description``.
        """
        for bridge in bridges:
            from_state = bridge["from_state"]
            to_state = bridge["to_state"]
            description = bridge.get("description", f"Bridge: {from_state} → {to_state}")

            # Derive a readable synthetic tool name
            bridge_name = f"bridge:{from_state}→{to_state}"

            synthetic_tool = ToolInfo(
                name=bridge_name,
                description=description,
                server_name="(bridge)",
                state_requires=(from_state,),
                state_produces=(to_state,),
            )

            self._tools.append(synthetic_tool)
            self._from_state[from_state].append(synthetic_tool)
            self._to_state[to_state].append(synthetic_tool)

            # Ensure both states appear in the domain vocabulary
            for token in (from_state, to_state):
                domain = self._domain_for_state(token)
                if domain and domain not in self._domain_states:
                    self._domain_states[domain] = {}
                if domain and token not in self._domain_states.get(domain, {}):
                    suffix = token.split(".", 1)[1] if "." in token else token
                    self._domain_states.setdefault(domain, {})[token] = suffix.upper()

    def _infer_states_from_tools(self) -> None:
        """Build a minimal state vocabulary from tool state annotations.

        Groups state tokens by domain prefix (e.g. 'chemistry.molecule_parsed'
        → domain 'chemistry') and creates readable labels from tokens.
        Uses State descriptions when available for richer labels.
        """
        all_tokens: set[str] = set()
        for tool in self._tools:
            all_tokens.update(tool.state_requires)
            all_tokens.update(tool.state_produces)

        # Also include skill state tokens
        for skill in self._skills:
            all_tokens.update(skill.get("states", []))

        # Group by domain prefix
        by_domain: dict[str, dict[str, str]] = defaultdict(dict)
        for token in sorted(all_tokens):
            domain = self._domain_for_state(token)
            # Use the State description if provided, otherwise generate a label
            if token in self._state_descriptions:
                label = self._state_descriptions[token]
            else:
                suffix = token.split(".", 1)[1] if "." in token else token
                label = suffix.upper()
            by_domain[domain or self._domain_name or "default"][token] = label

        for domain, states in by_domain.items():
            if domain not in self._domain_states:
                self._domain_states[domain] = states

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

        # Collect bridge edges (synthetic cross-domain transitions)
        bridge_edges: list[dict[str, Any]] = []
        for tool in self._tools:
            if tool.server_name == "(bridge)":
                for req in tool.state_requires:
                    for prod in tool.state_produces:
                        bridge_edges.append(
                            {
                                "from": req,
                                "to": prod,
                                "description": tool.description,
                            }
                        )

        response: dict[str, Any] = {"domains": result}
        if bridge_edges:
            response["bridges"] = bridge_edges
        return response

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
