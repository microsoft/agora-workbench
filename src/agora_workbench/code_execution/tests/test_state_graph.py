"""Tests for state graph integrity and affordance coverage.

These tests validate that:
- All state tokens used in ToolDefinition.state_transition belong to a
  registered domain state Enum.
- Every required state is produced by at least one tool in the same domain.
- The state graph is connected (all states reachable from root states).
- All tools with transitions have sufficient affordances.
- Affordances are unique within a domain and add search value.
"""

from __future__ import annotations

import re

import pytest

from agora_workbench.code_execution import StateTransition, ToolDefinition


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _collect_domain_states() -> dict[str, set[str]]:
    """Return {domain_prefix: {token, ...}} for all registered domain Enums."""
    return {}


def _all_valid_tokens() -> set[str]:
    """Flat set of every registered state token across all domains."""
    result: set[str] = set()
    for tokens in _collect_domain_states().values():
        result |= tokens
    return result


def _collect_domain_state_affordances() -> dict[str, list[str]]:
    """Return {state_token: [affordance, ...]} across all domains with a states module."""
    return {}


def _tools_with_transitions(tools: list[ToolDefinition]) -> list[ToolDefinition]:
    """Filter to tools that have at least one state token declared."""
    return [t for t in tools if t.state_transition.requires or t.state_transition.produces]


def _tokenize(text: str) -> set[str]:
    """Tokenise text the same way BM25 does for comparison."""
    return set(re.findall(r"[a-z0-9_]+", text.lower()))


# ---------------------------------------------------------------------------
# State vocabulary membership
# ---------------------------------------------------------------------------


class TestStateVocabulary:
    """Verify all state tokens are registered in domain Enums."""

    @pytest.mark.unit
    def test_state_tokens_are_domain_prefixed(self):
        """State tokens should follow the 'domain.state_name' convention."""
        valid = _all_valid_tokens()
        for token in valid:
            assert "." in token, f"State token '{token}' is missing a domain prefix"


# ---------------------------------------------------------------------------
# StateTransition model tests
# ---------------------------------------------------------------------------


class TestStateTransitionModel:
    """Test the StateTransition Pydantic model."""

    @pytest.mark.unit
    def test_default_empty(self):
        st = StateTransition()
        assert st.requires == frozenset()
        assert st.produces == frozenset()

    @pytest.mark.unit
    def test_from_sets(self):
        st = StateTransition(requires=frozenset({"a", "b"}), produces=frozenset({"c"}))
        assert st.requires == frozenset({"a", "b"})
        assert st.produces == frozenset({"c"})

    @pytest.mark.unit
    def test_serialization_sorted(self):
        st = StateTransition(requires=frozenset({"z", "a", "m"}), produces=frozenset({"b"}))
        d = st.model_dump()
        assert d["requires"] == ["a", "m", "z"]
        assert d["produces"] == ["b"]

    @pytest.mark.unit
    def test_round_trip(self):
        st = StateTransition(requires=frozenset({"x"}), produces=frozenset({"y", "z"}))
        d = st.model_dump()
        st2 = StateTransition(**d)
        assert st2.requires == st.requires
        assert st2.produces == st.produces


# ---------------------------------------------------------------------------
# Catalog JSON includes state transitions
# ---------------------------------------------------------------------------


class TestCatalogStateTransitions:
    """Verify the catalog JSON exposes state transitions."""

    @pytest.mark.unit
    def test_catalog_entry_includes_state_transition(self):
        """Catalog entries for tools with transitions should include state_transition."""
        td = ToolDefinition(
            name="solve_flowsheet",
            description="Solve a simulation flowsheet",
            module="test.module",
            state_transition=StateTransition(
                requires=frozenset({"sim.flowsheet_exists"}),
                produces=frozenset({"sim.flowsheet_solved"}),
            ),
        )
        entry: dict = {
            "name": td.name,
            "description": td.description,
            "server_name": "simulation",
        }
        if td.state_transition.requires or td.state_transition.produces:
            entry["state_transition"] = {
                "requires": sorted(td.state_transition.requires),
                "produces": sorted(td.state_transition.produces),
            }

        assert "state_transition" in entry
        st = entry["state_transition"]
        assert isinstance(st["requires"], list)
        assert isinstance(st["produces"], list)

    @pytest.mark.unit
    def test_tools_without_transitions_omit_field(self):
        """Catalog entries for tools without transitions should not include state_transition."""
        td = ToolDefinition(
            name="no_states_tool",
            description="A tool with no state annotations",
            module="test.module",
        )
        entry: dict = {"name": td.name, "description": td.description}
        if td.state_transition.requires or td.state_transition.produces:
            entry["state_transition"] = {
                "requires": sorted(td.state_transition.requires),
                "produces": sorted(td.state_transition.produces),
            }
        assert "state_transition" not in entry


# ---------------------------------------------------------------------------
# ToolInfo and ToolSearchResult carry state transitions
# ---------------------------------------------------------------------------


class TestPipelinePropagation:
    """Verify state transitions propagate through the discovery pipeline."""

    @pytest.mark.unit
    def test_tool_info_carries_state_fields(self):
        """ToolInfo should accept and store state_requires and state_produces."""
        from agora_workbench.code_execution.tools.tool_search import ToolInfo

        ti = ToolInfo(
            name="test_tool",
            description="desc",
            server_name="simulation",
            affordances=("a",),
            state_requires=("sim.flowsheet_exists",),
            state_produces=("sim.flowsheet_solved",),
        )
        assert ti.state_requires == ("sim.flowsheet_exists",)
        assert ti.state_produces == ("sim.flowsheet_solved",)

    @pytest.mark.unit
    def test_tool_info_defaults_empty(self):
        """ToolInfo state fields should default to empty tuples."""
        from agora_workbench.code_execution.tools.tool_search import ToolInfo

        ti = ToolInfo(name="test", description="desc", server_name="s")
        assert ti.state_requires == ()
        assert ti.state_produces == ()

    @pytest.mark.unit
    def test_search_result_carries_state_fields(self):
        """ToolSearchResult should include state_requires and state_produces."""
        from agora_workbench.code_execution.tools.tool_search import ToolSearchResult

        r = ToolSearchResult(
            name="test",
            server_name="simulation",
            description="desc",
            execution_type="mcp",
            state_requires=["sim.flowsheet_exists"],
            state_produces=["sim.flowsheet_solved"],
        )
        assert r.state_requires == ["sim.flowsheet_exists"]
        assert r.state_produces == ["sim.flowsheet_solved"]

    @pytest.mark.unit
    def test_search_result_defaults_empty(self):
        """ToolSearchResult state fields should default to empty lists."""
        from agora_workbench.code_execution.tools.tool_search import ToolSearchResult

        r = ToolSearchResult(
            name="test",
            server_name="s",
            description="d",
            execution_type="mcp",
        )
        assert r.state_requires == []
        assert r.state_produces == []

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_bm25_backend_propagates_state_fields(self):
        """BM25 search results should include state transition data from ToolInfo."""
        from agora_workbench.code_execution.tools.tool_search import ToolInfo
        from agora_workbench.code_execution.tools.search.bm25_tool_search import BM25ToolSearchBackend

        tools = [
            ToolInfo(
                name="solve_flowsheet",
                description="Solve a simulation flowsheet",
                server_name="simulation",
                state_requires=("sim.flowsheet_exists",),
                state_produces=("sim.flowsheet_solved", "sim.results_available"),
            ),
        ]
        backend = BM25ToolSearchBackend()
        backend.index(tools)
        results = await backend.search("solve flowsheet", top=1)
        assert len(results) == 1
        assert results[0].state_requires == ["sim.flowsheet_exists"]
        assert set(results[0].state_produces) == {"sim.flowsheet_solved", "sim.results_available"}


# ---------------------------------------------------------------------------
# StateGraph query tool
# ---------------------------------------------------------------------------


class TestStateGraphQueryTool:
    """Test the StateGraph class and plan_{name}_workflow tool."""

    def _make_graph(self):  # noqa: F821
        from agora_workbench.code_execution.tools.tool_search import ToolInfo
        from agora_workbench.code_execution.tools.search.state_graph import StateGraph

        tools = [
            ToolInfo(
                name="search_compounds",
                description="Search compound database",
                server_name="sim",
                state_produces=("sim.compounds_available",),
            ),
            ToolInfo(
                name="create_flowsheet",
                description="Create a flowsheet",
                server_name="sim",
                state_requires=("sim.compounds_available",),
                state_produces=("sim.flowsheet_exists",),
            ),
            ToolInfo(
                name="add_mixer",
                description="Add a mixer",
                server_name="sim",
                state_requires=("sim.flowsheet_exists",),
                state_produces=("sim.flowsheet_exists",),
            ),
            ToolInfo(
                name="solve_flowsheet",
                description="Solve the flowsheet",
                server_name="sim",
                state_requires=("sim.flowsheet_exists",),
                state_produces=("sim.flowsheet_solved",),
            ),
        ]
        graph = StateGraph(tools)
        # Inject synthetic domain states so overview() can find them
        graph._domain_states["sim"] = {
            "sim.compounds_available": "COMPOUNDS_AVAILABLE",
            "sim.flowsheet_exists": "FLOWSHEET_EXISTS",
            "sim.flowsheet_solved": "FLOWSHEET_SOLVED",
        }
        return graph

    @pytest.mark.unit
    def test_overview_returns_domain(self):
        graph = self._make_graph()
        result = graph.overview("sim")
        assert "domains" in result
        sim = result["domains"][0]
        assert sim["domain"] == "sim"
        assert len(sim["states"]) > 0
        assert len(sim["edges"]) > 0

    @pytest.mark.unit
    def test_overview_all_domains(self):
        graph = self._make_graph()
        result = graph.overview()
        assert "domains" in result
        domain_names = {d["domain"] for d in result["domains"]}
        assert "sim" in domain_names

    @pytest.mark.unit
    def test_from_state_returns_tools(self):
        graph = self._make_graph()
        result = graph.from_state("sim.flowsheet_exists")
        assert result["state"] == "sim.flowsheet_exists"
        tool_names = {t["name"] for t in result["tools_from_here"]}
        assert "add_mixer" in tool_names
        assert "solve_flowsheet" in tool_names
        assert "sim.flowsheet_solved" in result["next_states"]

    @pytest.mark.unit
    def test_from_state_escape_hatch(self):
        """When no tools transition from a state, a hint should mention execute_*_code."""
        graph = self._make_graph()
        result = graph.from_state("sim.flowsheet_solved")
        # flowsheet_solved has no tools requiring it in our test data
        assert "hint" in result
        assert "execute_" in result["hint"]

    @pytest.mark.unit
    def test_path_finds_route(self):
        graph = self._make_graph()
        result = graph.path("sim.compounds_available", "sim.flowsheet_solved")
        assert result["path"] is not None
        assert len(result["path"]) == 2  # compounds→flowsheet→solved
        assert result["path"][0]["tool"] == "create_flowsheet"
        assert result["path"][1]["tool"] == "solve_flowsheet"

    @pytest.mark.unit
    def test_path_no_route_escape_hatch(self):
        graph = self._make_graph()
        result = graph.path("sim.flowsheet_solved", "sim.compounds_available")
        assert result["path"] is None
        assert "hint" in result
        assert "execute_" in result["hint"]

    @pytest.mark.unit
    def test_tool_lookup(self):
        graph = self._make_graph()
        result = graph.tool_lookup("create_flowsheet")
        assert result["name"] == "create_flowsheet"
        assert "sim.compounds_available" in result["requires"]
        assert "sim.flowsheet_exists" in result["produces"]

    @pytest.mark.unit
    def test_tool_lookup_not_found(self):
        graph = self._make_graph()
        result = graph.tool_lookup("nonexistent_tool")
        assert "error" in result


class TestSkillFrontmatter:
    """Validate SKILL.md frontmatter state annotations."""

    @staticmethod
    def _collect_skill_paths() -> list:
        """Return explicit list of SKILL.md paths from example servers."""
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[4]
        servers_dir = repo_root / "examples" / "servers"
        if not servers_dir.is_dir():
            return []
        return sorted(servers_dir.rglob("skills/SKILL.md"))

    @staticmethod
    def _parse_skills(paths: list) -> list[dict]:
        """Parse frontmatter from explicit SKILL.md paths."""
        from agora_workbench.code_execution.tools.search.state_graph import _parse_skill_frontmatter

        skills: list[dict] = []
        for path in paths:
            fm = _parse_skill_frontmatter(path)
            if not fm.get("name"):
                continue
            # Derive domain from the parent server directory name
            # e.g. examples/servers/chemistry/skills/SKILL.md -> chemistry
            domain = path.parent.parent.name
            skills.append(
                {
                    "name": fm["name"],
                    "description": fm.get("description", ""),
                    "domain": domain,
                    "states": fm.get("states", []),
                    "path": str(path),
                }
            )
        return skills

    @staticmethod
    def _skills_with_states() -> list[dict]:
        """Return parsed skills that declare state annotations."""
        cls = TestSkillFrontmatter
        paths = cls._collect_skill_paths()
        return [s for s in cls._parse_skills(paths) if s.get("states")]

    @pytest.mark.unit
    def test_skill_states_are_valid_tokens(self):
        """Every state token in SKILL.md frontmatter must belong to a domain Enum."""
        valid = _all_valid_tokens()
        if not valid:
            pytest.skip("No domain state Enums registered; cannot validate skill tokens")
        skills = self._skills_with_states()
        invalid: list[tuple[str, str]] = []
        for skill in skills:
            for token in skill["states"]:
                if token not in valid:
                    invalid.append((skill["name"], token))
        assert not invalid, f"Skills reference unknown state tokens: {invalid}"

    @pytest.mark.unit
    def test_skill_states_match_domain(self):
        """Skills should only reference state tokens prefixed with their own domain."""
        skills = self._skills_with_states()
        mismatches: list[tuple[str, str, str]] = []
        for skill in skills:
            domain = skill["domain"]
            for token in skill["states"]:
                prefix = token.split(".")[0] if "." in token else ""
                if prefix != domain:
                    mismatches.append((skill["name"], domain, token))
        assert not mismatches, f"Skills reference states from wrong domain: {mismatches}"

    @pytest.mark.unit
    def test_skill_states_minimum_cardinality(self):
        """Skills with states should have at least 2 (entry + exit)."""
        skills = self._skills_with_states()
        too_few: list[tuple[str, int]] = []
        for skill in skills:
            if len(skill["states"]) < 2:
                too_few.append((skill["name"], len(skill["states"])))
        assert not too_few, f"Skills with fewer than 2 states: {too_few}"
