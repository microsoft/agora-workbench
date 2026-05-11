"""Tests for BM25-based tool search backend."""

import pytest

from tools.search.bm25_tool_search import (
    BM25ToolSearchBackend,
    _tool_info_text,
)
from tools.search.build_tool_list import ToolInfo


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
        backend = BM25ToolSearchBackend(tools=sample_tools)
        results = await backend.search("run_opf", top=1)
        assert len(results) == 1
        assert results[0].name == "run_opf"
        assert results[0].score > 0

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_search_finds_by_description(self, sample_tools):
        backend = BM25ToolSearchBackend(tools=sample_tools)
        results = await backend.search("optimal power flow", top=1)
        assert results[0].name == "run_opf"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_search_respects_top(self, sample_tools):
        backend = BM25ToolSearchBackend(tools=sample_tools)
        results = await backend.search("network", top=2)
        assert len(results) == 2
        assert results[0].score is not None and results[1].score is not None
        assert results[0].score >= results[1].score

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_search_empty_tools(self):
        backend = BM25ToolSearchBackend(tools=[])
        results = await backend.search("query")
        assert results == []

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_search_empty_query(self, sample_tools):
        backend = BM25ToolSearchBackend(tools=sample_tools)
        results = await backend.search("")
        assert results == []

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_result_fields(self, sample_tools):
        backend = BM25ToolSearchBackend(tools=sample_tools)
        results = await backend.search("run_opf", top=1)
        r = results[0]
        assert r.name == "run_opf"
        assert r.server_name == "powergrid"
        assert r.execution_type == "mcp"
        assert isinstance(r.score, float)
