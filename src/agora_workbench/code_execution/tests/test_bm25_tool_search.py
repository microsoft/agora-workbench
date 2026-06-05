"""Tests for BM25-based tool search backend."""

import pytest

from agora_workbench.code_execution.tools.search.bm25_tool_search import (
    BM25ToolSearchBackend,
    _tool_info_text,
    _skill_info_text,
)
from agora_workbench.code_execution.tools.tool_search import ToolInfo


class TestToolInfoText:
    """Test the text-extraction helper for ToolInfo documents."""

    @pytest.mark.unit
    def test_includes_name_and_description(self):
        tool = ToolInfo(name="run_opf", description="Run optimal power flow", server_name="s")
        text = _tool_info_text(tool)
        assert "run_opf" in text
        assert "Run optimal power flow" in text

    @pytest.mark.unit
    def test_includes_affordances(self):
        tool = ToolInfo(
            name="t",
            description="d",
            server_name="s",
            affordances=("read", "write"),
        )
        text = _tool_info_text(tool)
        assert "read" in text
        assert "write" in text

    @pytest.mark.unit
    def test_empty_fields(self):
        tool = ToolInfo(name="", description="", server_name="s")
        text = _tool_info_text(tool)
        assert isinstance(text, str)


class TestBM25ToolSearchBackend:
    """Test cases for BM25ToolSearchBackend."""

    @pytest.fixture
    def sample_tools(self):
        return [
            ToolInfo(
                name="run_opf",
                description="Run optimal power flow analysis on a network",
                server_name="powergrid",
            ),
            ToolInfo(
                name="build_network",
                description="Build a power network topology from data",
                server_name="powergrid",
            ),
            ToolInfo(
                name="analyze_results",
                description="Analyze simulation results and generate reports",
                server_name="powergrid",
            ),
        ]

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_search_finds_by_name(self, sample_tools):
        backend = BM25ToolSearchBackend()
        backend.index(tools=sample_tools)
        results = await backend.search("run_opf", top=1)
        assert len(results) == 1
        assert results[0].name == "run_opf"
        assert results[0].score > 0

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_search_finds_by_description(self, sample_tools):
        backend = BM25ToolSearchBackend()
        backend.index(tools=sample_tools)
        results = await backend.search("optimal power flow", top=1)
        assert results[0].name == "run_opf"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_search_respects_top(self, sample_tools):
        backend = BM25ToolSearchBackend()
        backend.index(tools=sample_tools)
        results = await backend.search("network", top=2)
        assert len(results) == 2
        assert results[0].score is not None and results[1].score is not None
        assert results[0].score >= results[1].score

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_search_empty_tools(self):
        backend = BM25ToolSearchBackend()
        backend.index(tools=[])
        results = await backend.search("query")
        assert results == []

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_search_empty_query_dumps_catalog(self, sample_tools):
        """Empty query is the documented catalog-dump path (see ``search()`` docstring)."""
        backend = BM25ToolSearchBackend()
        backend.index(tools=sample_tools)
        results = await backend.search("", top=10)
        assert {r.name for r in results} == {t.name for t in sample_tools}
        assert all(r.score == 0.0 for r in results)

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_result_fields(self, sample_tools):
        backend = BM25ToolSearchBackend()
        backend.index(tools=sample_tools, server_name="powergrid")
        results = await backend.search("run_opf", top=1)
        r = results[0]
        assert r.name == "run_opf"
        assert r.server_name == "powergrid"
        assert r.execution_type == "mcp"
        assert r.type == "tool"
        assert r.to_access == "Call via execute_powergrid_code"
        assert isinstance(r.score, float)


class TestSkillInfoText:
    """Test the text-extraction helper for skill metadata dicts."""

    @pytest.mark.unit
    def test_includes_name_and_description(self):
        skill = {"name": "drug-screening", "description": "Drug-likeness evaluation"}
        text = _skill_info_text(skill)
        assert "drug-screening" in text
        assert "Drug-likeness evaluation" in text

    @pytest.mark.unit
    def test_includes_states(self):
        skill = {
            "name": "test-skill",
            "description": "desc",
            "states": ["chem.parsed", "chem.filtered"],
        }
        text = _skill_info_text(skill)
        assert "chem.parsed" in text
        assert "chem.filtered" in text

    @pytest.mark.unit
    def test_empty_skill(self):
        text = _skill_info_text({})
        assert isinstance(text, str)


class TestBM25SkillSearch:
    """Test skill indexing and category filtering in BM25ToolSearchBackend."""

    @pytest.fixture
    def sample_tools(self):
        return [
            ToolInfo(
                name="compute_descriptors",
                description="Compute molecular descriptors",
                server_name="chemistry",
            ),
            ToolInfo(
                name="filter_drug_candidates",
                description="Filter molecules for drug-likeness",
                server_name="chemistry",
            ),
        ]

    @pytest.fixture
    def sample_skills(self):
        return [
            {
                "name": "drug-screening",
                "description": "Drug-likeness evaluation using Lipinski rules",
                "domain": "chemistry",
                "states": ["chemistry.descriptors_computed", "chemistry.candidates_filtered"],
            },
            {
                "name": "molecular-analysis",
                "description": "Structural characterization of molecules",
                "domain": "chemistry",
                "states": ["chemistry.molecule_parsed", "chemistry.groups_identified"],
            },
        ]

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_skills_searchable_by_name(self, sample_tools, sample_skills):
        backend = BM25ToolSearchBackend()
        backend.index(tools=sample_tools, skills=sample_skills, server_name="chemistry")
        results = await backend.search("drug-screening", top=5)
        skill_results = [r for r in results if r.type == "skill"]
        assert len(skill_results) >= 1
        assert skill_results[0].name == "drug-screening"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_skills_searchable_by_description(self, sample_tools, sample_skills):
        backend = BM25ToolSearchBackend()
        backend.index(tools=sample_tools, skills=sample_skills, server_name="chemistry")
        results = await backend.search("Lipinski", top=5)
        skill_results = [r for r in results if r.type == "skill"]
        assert len(skill_results) >= 1
        assert skill_results[0].name == "drug-screening"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_category_tools_excludes_skills(self, sample_tools, sample_skills):
        backend = BM25ToolSearchBackend()
        backend.index(tools=sample_tools, skills=sample_skills, server_name="chemistry")
        results = await backend.search("drug", top=5, category="tools")
        assert all(r.type == "tool" for r in results)

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_category_skills_excludes_tools(self, sample_tools, sample_skills):
        backend = BM25ToolSearchBackend()
        backend.index(tools=sample_tools, skills=sample_skills, server_name="chemistry")
        results = await backend.search("drug", top=5, category="skills")
        assert all(r.type == "skill" for r in results)

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_category_all_returns_both(self, sample_tools, sample_skills):
        backend = BM25ToolSearchBackend()
        backend.index(tools=sample_tools, skills=sample_skills, server_name="chemistry")
        results = await backend.search("drug", top=5, category="all")
        types = {r.type for r in results}
        assert "tool" in types
        assert "skill" in types

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_skill_result_has_to_access(self, sample_tools, sample_skills):
        backend = BM25ToolSearchBackend()
        backend.index(tools=sample_tools, skills=sample_skills, server_name="chemistry")
        results = await backend.search("drug-screening", top=5, category="skills")
        assert len(results) >= 1
        r = results[0]
        assert r.type == "skill"
        assert "load_chemistry_skill" in r.to_access
        assert "drug-screening" in r.to_access

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_skill_result_fields(self, sample_tools, sample_skills):
        backend = BM25ToolSearchBackend()
        backend.index(tools=sample_tools, skills=sample_skills, server_name="chemistry")
        results = await backend.search("drug-screening", top=1, category="skills")
        r = results[0]
        assert r.name == "drug-screening"
        assert r.server_name == "chemistry"
        assert r.execution_type == "skill"
        assert r.type == "skill"
        assert r.state_requires == []
        assert r.state_produces == []

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_no_skills_returns_empty_for_skill_category(self, sample_tools):
        backend = BM25ToolSearchBackend()
        backend.index(tools=sample_tools, server_name="chemistry")
        results = await backend.search("drug", top=5, category="skills")
        assert results == []

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_results_sorted_by_score(self, sample_tools, sample_skills):
        backend = BM25ToolSearchBackend()
        backend.index(tools=sample_tools, skills=sample_skills, server_name="chemistry")
        results = await backend.search("drug", top=10, category="all")
        scores = [r.score for r in results]
        assert scores == sorted(scores, key=lambda s: s if s is not None else 0.0, reverse=True)

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_empty_query_returns_full_catalog(self, sample_tools, sample_skills):
        """Empty query is the documented catalog-dump path; must return every indexed item."""
        backend = BM25ToolSearchBackend()
        backend.index(tools=sample_tools, skills=sample_skills, server_name="chemistry")
        results = await backend.search("", top=999, category="all")
        names = {r.name for r in results}
        assert names == {t.name for t in sample_tools} | {s["name"] for s in sample_skills}
        # Catalog dump entries have score 0 — agents shouldn't read it as a ranking signal.
        assert all(r.score == 0.0 for r in results)

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_empty_query_respects_top(self, sample_tools, sample_skills):
        backend = BM25ToolSearchBackend()
        backend.index(tools=sample_tools, skills=sample_skills, server_name="chemistry")
        results = await backend.search("", top=1, category="all")
        # ``top`` caps each category independently, so we expect 1 tool + 1 skill.
        types = sorted(r.type for r in results)
        assert types == ["skill", "tool"]

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_whitespace_query_returns_full_catalog(self, sample_tools):
        backend = BM25ToolSearchBackend()
        backend.index(tools=sample_tools, server_name="chemistry")
        results = await backend.search("   ", top=10, category="tools")
        assert {r.name for r in results} == {t.name for t in sample_tools}
