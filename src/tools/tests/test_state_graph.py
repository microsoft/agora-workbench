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

from code_execution import StateTransition, ToolDefinition


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _collect_domain_states() -> dict[str, set[str]]:
    """Return {domain_prefix: {token, ...}} for all registered domain Enums."""
    states: dict[str, set[str]] = {}

    from domains.dwsim.states import DwsimState

    states["dwsim"] = {s.value for s in DwsimState}

    from domains.powergrid.states import PowergridState

    states["powergrid"] = {s.value for s in PowergridState}

    from domains.gis.states import GisState

    states["gis"] = {s.value for s in GisState}

    from domains.office.states import OfficeState

    states["office"] = {s.value for s in OfficeState}

    from domains.openlca.states import OpenlcaState

    states["openlca"] = {s.value for s in OpenlcaState}

    from domains.process.states import ProcessState

    states["process"] = {s.value for s in ProcessState}

    from domains.foundry.states import FoundryState

    states["foundry"] = {s.value for s in FoundryState}

    from domains.example.states import ExampleState

    states["example"] = {s.value for s in ExampleState}

    from domains.latex.states import LatexState

    states["latex"] = {s.value for s in LatexState}

    return states


def _all_valid_tokens() -> set[str]:
    """Flat set of every registered state token across all domains."""
    result: set[str] = set()
    for tokens in _collect_domain_states().values():
        result |= tokens
    return result


def _collect_domain_state_affordances() -> dict[str, list[str]]:
    """Return {state_token: [affordance, ...]} across all domains with a states module."""
    import importlib

    mapping: dict[str, list[str]] = {}
    domains = _collect_domain_states()
    for domain_name in domains:
        try:
            mod = importlib.import_module(f"domains.{domain_name}.states")
            raw = getattr(mod, "STATE_AFFORDANCES", {})
            for state_enum, phrases in raw.items():
                mapping[state_enum.value] = phrases
        except (ImportError, AttributeError):
            pass
    return mapping


def _load_dwsim_tools() -> list[ToolDefinition]:
    """Load all DWSIM tool definitions."""
    from domains.dwsim.server.tool_registry import create_dwsim_tool_registry

    registry = create_dwsim_tool_registry()
    return list(registry.tools)


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
    def test_state_tokens_are_registered(self):
        """Every token in any ToolDefinition.state_transition must belong to a domain Enum."""
        valid = _all_valid_tokens()
        tools = _load_dwsim_tools()
        unknown: list[tuple[str, str]] = []
        for t in tools:
            for token in t.state_transition.requires | t.state_transition.produces:
                if token not in valid:
                    unknown.append((t.name, token))
        assert not unknown, f"Unregistered state tokens: {unknown}"

    @pytest.mark.unit
    def test_state_tokens_are_domain_prefixed(self):
        """State tokens should follow the 'domain.state_name' convention."""
        valid = _all_valid_tokens()
        for token in valid:
            assert "." in token, f"State token '{token}' is missing a domain prefix"


# ---------------------------------------------------------------------------
# Graph structural invariants
# ---------------------------------------------------------------------------


class TestStateGraph:
    """Validate structural integrity of the state graph."""

    @pytest.mark.unit
    def test_no_orphan_required_states(self):
        """Every required state must be produced by at least one tool."""
        tools = _load_dwsim_tools()
        all_produced: set[str] = set()
        all_required: set[str] = set()
        for t in tools:
            all_produced |= t.state_transition.produces
            all_required |= t.state_transition.requires
        orphans = all_required - all_produced
        assert not orphans, f"States required but never produced: {orphans}"

    @pytest.mark.unit
    def test_no_unreachable_states(self):
        """From root states (no prerequisites), all produced states should be reachable."""
        tools = _load_dwsim_tools()
        annotated = _tools_with_transitions(tools)
        if not annotated:
            pytest.skip("No tools with state transitions")

        # Build adjacency: for each tool, produces are reachable from requires
        all_states: set[str] = set()
        for t in annotated:
            all_states |= t.state_transition.requires | t.state_transition.produces

        # Root tools: those with empty requires (or all requires are empty)
        reachable: set[str] = set()
        changed = True
        while changed:
            changed = False
            for t in annotated:
                if t.state_transition.requires <= reachable or not t.state_transition.requires:
                    new = t.state_transition.produces - reachable
                    if new:
                        reachable |= new
                        changed = True

        unreachable = all_states - reachable
        # Filter to only states that are produced (not merely required)
        all_produced = set()
        for t in annotated:
            all_produced |= t.state_transition.produces
        unreachable_produced = unreachable & all_produced
        assert not unreachable_produced, f"Produced states unreachable from roots: {unreachable_produced}"

    @pytest.mark.unit
    def test_dwsim_has_state_transitions(self):
        """The DWSIM domain should have tools with state transitions."""
        tools = _load_dwsim_tools()
        annotated = _tools_with_transitions(tools)
        assert len(annotated) >= 20, f"Expected at least 20 DWSIM tools with state transitions, got {len(annotated)}"


# ---------------------------------------------------------------------------
# Affordance coverage & quality
# ---------------------------------------------------------------------------


class TestAffordances:
    """Validate affordance coverage and quality."""

    @pytest.mark.unit
    def test_all_annotated_tools_have_affordances(self):
        """Every tool with state transitions should have >= 2 effective affordances."""
        tools = _load_dwsim_tools()
        state_affs = _collect_domain_state_affordances()
        insufficient: list[tuple[str, int]] = []
        for t in _tools_with_transitions(tools):
            # Compute effective affordances (state-derived + tool-specific)
            effective: list[str] = []
            for token in t.state_transition.produces:
                effective.extend(state_affs.get(token, []))
            effective.extend(t.affordances)
            if len(set(effective)) < 2:
                insufficient.append((t.name, len(set(effective))))
        assert not insufficient, f"Tools with < 2 effective affordances: {insufficient}"

    @pytest.mark.unit
    def test_affordances_unique_within_domain(self):
        """No two tools in the same domain should share identical affordance strings."""
        tools = _load_dwsim_tools()
        seen: dict[str, str] = {}
        duplicates: list[tuple[str, str, str]] = []
        for t in tools:
            for aff in t.affordances:
                key = aff.strip().lower()
                if key in seen and seen[key] != t.name:
                    duplicates.append((aff, t.name, seen[key]))
                seen[key] = t.name
        assert not duplicates, f"Duplicate affordances: {duplicates}"

    @pytest.mark.unit
    def test_affordances_add_search_value(self):
        """Each tool-specific affordance should contribute at least one novel token."""
        tools = _load_dwsim_tools()
        low_value: list[tuple[str, str]] = []
        for t in tools:
            desc_tokens = _tokenize(t.name + " " + t.description)
            for aff in t.affordances:
                aff_tokens = _tokenize(aff)
                novel = aff_tokens - desc_tokens
                if not novel:
                    low_value.append((t.name, aff))
        assert not low_value, f"Affordances that add no novel search tokens beyond the description: {low_value}"

    @pytest.mark.unit
    def test_state_affordances_cover_all_states(self):
        """Every DWSIM state enum value should have at least one affordance phrase."""
        from domains.dwsim.states import DwsimState, STATE_AFFORDANCES

        missing = []
        for state in DwsimState:
            if state not in STATE_AFFORDANCES or not STATE_AFFORDANCES[state]:
                missing.append(state.value)
        assert not missing, f"States with no affordances: {missing}"


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

        # Build a minimal catalog entry using the server's serialization logic.
        tools = _load_dwsim_tools()
        annotated = _tools_with_transitions(tools)
        assert annotated, "Expected at least one DWSIM tool with state transitions"

        # Simulate what server.py does — build a catalog entry dict.
        td = annotated[0]
        entry: dict = {
            "name": td.name,
            "description": td.description,
            "server_name": "dwsim",
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
        from tools.search.build_tool_list import ToolInfo

        ti = ToolInfo(
            name="test_tool",
            description="desc",
            server_name="dwsim",
            affordances=("a",),
            state_requires=("dwsim.flowsheet_exists",),
            state_produces=("dwsim.flowsheet_solved",),
        )
        assert ti.state_requires == ("dwsim.flowsheet_exists",)
        assert ti.state_produces == ("dwsim.flowsheet_solved",)

    @pytest.mark.unit
    def test_tool_info_defaults_empty(self):
        """ToolInfo state fields should default to empty tuples."""
        from tools.search.build_tool_list import ToolInfo

        ti = ToolInfo(name="test", description="desc", server_name="s")
        assert ti.state_requires == ()
        assert ti.state_produces == ()

    @pytest.mark.unit
    def test_search_result_carries_state_fields(self):
        """ToolSearchResult should include state_requires and state_produces."""
        from tools.tool_search import ToolSearchResult

        r = ToolSearchResult(
            name="test",
            server_name="dwsim",
            description="desc",
            execution_type="mcp",
            state_requires=["dwsim.flowsheet_exists"],
            state_produces=["dwsim.flowsheet_solved"],
        )
        assert r.state_requires == ["dwsim.flowsheet_exists"]
        assert r.state_produces == ["dwsim.flowsheet_solved"]

    @pytest.mark.unit
    def test_search_result_defaults_empty(self):
        """ToolSearchResult state fields should default to empty lists."""
        from tools.tool_search import ToolSearchResult

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
        from tools.search.build_tool_list import ToolInfo
        from tools.search.bm25_tool_search import BM25ToolSearchBackend

        tools = [
            ToolInfo(
                name="solve_flowsheet",
                description="Solve a DWSIM flowsheet",
                server_name="dwsim",
                state_requires=("dwsim.flowsheet_exists",),
                state_produces=("dwsim.flowsheet_solved", "dwsim.results_available"),
            ),
        ]
        backend = BM25ToolSearchBackend(tools)
        results = await backend.search("solve flowsheet", top=1)
        assert len(results) == 1
        assert results[0].state_requires == ["dwsim.flowsheet_exists"]
        assert set(results[0].state_produces) == {"dwsim.flowsheet_solved", "dwsim.results_available"}


# ---------------------------------------------------------------------------
# StateGraph query tool
# ---------------------------------------------------------------------------


class TestStateGraphQueryTool:
    """Test the StateGraph class and query_state_graph FunctionTool."""

    def _make_graph(self):  # noqa: F821
        from tools.search.build_tool_list import ToolInfo
        from tools.search.state_graph import StateGraph

        tools = [
            ToolInfo(
                name="search_compounds",
                description="Search compound database",
                server_name="dwsim",
                state_produces=("dwsim.compounds_available",),
            ),
            ToolInfo(
                name="create_flowsheet",
                description="Create a flowsheet",
                server_name="dwsim",
                state_requires=("dwsim.compounds_available",),
                state_produces=("dwsim.flowsheet_exists",),
            ),
            ToolInfo(
                name="add_mixer",
                description="Add a mixer",
                server_name="dwsim",
                state_requires=("dwsim.flowsheet_exists",),
                state_produces=("dwsim.flowsheet_exists",),
            ),
            ToolInfo(
                name="solve_flowsheet",
                description="Solve the flowsheet",
                server_name="dwsim",
                state_requires=("dwsim.flowsheet_exists",),
                state_produces=("dwsim.flowsheet_solved",),
            ),
        ]
        return StateGraph(tools)

    @pytest.mark.unit
    def test_overview_returns_dwsim_domain(self):
        graph = self._make_graph()
        result = graph.overview("dwsim")
        assert "domains" in result
        dwsim = result["domains"][0]
        assert dwsim["domain"] == "dwsim"
        assert len(dwsim["states"]) > 0
        assert len(dwsim["edges"]) > 0

    @pytest.mark.unit
    def test_overview_all_domains(self):
        graph = self._make_graph()
        result = graph.overview()
        assert "domains" in result
        domain_names = {d["domain"] for d in result["domains"]}
        assert "dwsim" in domain_names

    @pytest.mark.unit
    def test_from_state_returns_tools(self):
        graph = self._make_graph()
        result = graph.from_state("dwsim.flowsheet_exists")
        assert result["state"] == "dwsim.flowsheet_exists"
        tool_names = {t["name"] for t in result["tools_from_here"]}
        assert "add_mixer" in tool_names
        assert "solve_flowsheet" in tool_names
        assert "dwsim.flowsheet_solved" in result["next_states"]

    @pytest.mark.unit
    def test_from_state_escape_hatch(self):
        """When no tools transition from a state, a hint should mention execute_*_code."""
        graph = self._make_graph()
        result = graph.from_state("dwsim.flowsheet_solved")
        # flowsheet_solved has no tools requiring it in our test data
        assert "hint" in result
        assert "execute_" in result["hint"]

    @pytest.mark.unit
    def test_path_finds_route(self):
        graph = self._make_graph()
        result = graph.path("dwsim.compounds_available", "dwsim.flowsheet_solved")
        assert result["path"] is not None
        assert len(result["path"]) == 2  # compounds→flowsheet→solved
        assert result["path"][0]["tool"] == "create_flowsheet"
        assert result["path"][1]["tool"] == "solve_flowsheet"

    @pytest.mark.unit
    def test_path_no_route_escape_hatch(self):
        graph = self._make_graph()
        result = graph.path("dwsim.flowsheet_solved", "dwsim.compounds_available")
        assert result["path"] is None
        assert "hint" in result
        assert "execute_" in result["hint"]

    @pytest.mark.unit
    def test_tool_lookup(self):
        graph = self._make_graph()
        result = graph.tool_lookup("create_flowsheet")
        assert result["name"] == "create_flowsheet"
        assert "dwsim.compounds_available" in result["requires"]
        assert "dwsim.flowsheet_exists" in result["produces"]

    @pytest.mark.unit
    def test_tool_lookup_not_found(self):
        graph = self._make_graph()
        result = graph.tool_lookup("nonexistent_tool")
        assert "error" in result

    @pytest.mark.unit
    def test_overview_includes_skills(self):
        """Overview should include skills with state annotations."""
        graph = self._make_graph()
        result = graph.overview("dwsim")
        dwsim = result["domains"][0]
        # Skills are discovered from the filesystem, so we check that
        # DWSIM skills with states annotations are included
        if dwsim["skills"]:
            for skill in dwsim["skills"]:
                assert "name" in skill
                assert "states" in skill
                assert len(skill["states"]) >= 2

    @pytest.mark.unit
    def test_from_state_includes_relevant_skills(self):
        """from_state should surface skills whose state range overlaps."""
        graph = self._make_graph()
        result = graph.from_state("dwsim.flowsheet_exists")
        # If skills are found, they should overlap with flowsheet_exists
        if "relevant_skills" in result:
            for skill in result["relevant_skills"]:
                assert "dwsim.flowsheet_exists" in skill["states"] or any(
                    s in result["next_states"] for s in skill["states"]
                )


# ---------------------------------------------------------------------------
# SKILL.md frontmatter validation
# ---------------------------------------------------------------------------


class TestSkillFrontmatter:
    """Validate SKILL.md frontmatter state annotations."""

    @staticmethod
    def _discover_skills_with_states() -> list[dict]:
        """Find all SKILL.md files with a states field in frontmatter."""
        from tools.search.state_graph import _discover_skills, _DOMAINS_DIR

        return [s for s in _discover_skills(_DOMAINS_DIR) if s.get("states")]

    @pytest.mark.unit
    def test_skill_states_are_valid_tokens(self):
        """Every state token in SKILL.md frontmatter must belong to a domain Enum."""
        valid = _all_valid_tokens()
        skills = self._discover_skills_with_states()
        invalid: list[tuple[str, str]] = []
        for skill in skills:
            for token in skill["states"]:
                if token not in valid:
                    invalid.append((skill["name"], token))
        assert not invalid, f"Skills reference unknown state tokens: {invalid}"

    @pytest.mark.unit
    def test_skill_states_match_domain(self):
        """Skills under domains/X/ should only reference X.* state tokens."""
        skills = self._discover_skills_with_states()
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
        skills = self._discover_skills_with_states()
        too_few: list[tuple[str, int]] = []
        for skill in skills:
            if len(skill["states"]) < 2:
                too_few.append((skill["name"], len(skill["states"])))
        assert not too_few, f"Skills with fewer than 2 states: {too_few}"

    @pytest.mark.unit
    def test_skill_state_range_is_connected(self):
        """The state range declared by a skill should be reachable in the tool graph."""
        from tools.search.state_graph import StateGraph
        from tools.search.build_tool_list import ToolInfo

        # Build graph from real DWSIM tools
        tools_defs = _load_dwsim_tools()
        tool_infos = [
            ToolInfo(
                name=td.name,
                description=td.description,
                server_name=td.server_name or "dwsim",
                state_requires=tuple(td.state_transition.requires),
                state_produces=tuple(td.state_transition.produces),
            )
            for td in tools_defs
            if td.state_transition.requires or td.state_transition.produces
        ]
        graph = StateGraph(tool_infos)

        # Build a set of co-produced state pairs (produced together by one tool)
        co_produced: set[frozenset[str]] = set()
        for td in tools_defs:
            prods = td.state_transition.produces
            if len(prods) > 1:
                for a in prods:
                    for b in prods:
                        if a != b:
                            co_produced.add(frozenset({a, b}))

        skills = self._discover_skills_with_states()
        disconnected: list[tuple[str, str, str]] = []
        for skill in skills:
            states = skill["states"]
            if len(states) >= 2:
                entry, exit_ = states[0], states[-1]
                if entry == exit_:
                    continue
                # Connected if: path exists OR states are co-produced
                path = graph._find_path(entry, exit_)
                are_co_produced = frozenset({entry, exit_}) in co_produced
                if path is None and not are_co_produced:
                    disconnected.append((skill["name"], entry, exit_))
        assert not disconnected, f"Skills with unreachable state ranges: {disconnected}"
