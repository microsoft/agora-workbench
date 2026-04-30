"""Tests for tools.search.core — search_tools factory."""

import json

import pytest
from unittest.mock import AsyncMock, MagicMock

from tools.tool_search import ToolSearchResult
from tools.search.core import (
    SearchToolsInput,
    create_search_tools_function,
)


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------


class TestInputModels:
    @pytest.mark.unit
    def test_search_tools_input_defaults(self):
        inp = SearchToolsInput(query="power flow")
        assert inp.query == "power flow"
        assert inp.top == 5


# ---------------------------------------------------------------------------
# create_search_tools_function
# ---------------------------------------------------------------------------


class TestCreateSearchToolsFunction:
    @pytest.mark.unit
    def test_tool_metadata(self):
        mock_backend = MagicMock()
        fn = create_search_tools_function(mock_backend)
        assert fn.name == "search_tools"
        assert "catalog" in fn.description.lower() or "search" in fn.description.lower()
        assert fn.approval_mode == "never_require"
        assert fn.input_model is SearchToolsInput

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_returns_json_array(self):
        results = [
            ToolSearchResult(
                name="run_opf",
                server_name="powergrid",
                description="Run OPF",
                execution_type="mcp",
                score=1.5,
            ),
            ToolSearchResult(
                name="build_network",
                server_name="",
                description="Build network",
                execution_type="mcp",
                score=0.8,
            ),
        ]
        mock_backend = MagicMock()
        mock_backend.search = AsyncMock(return_value=results)

        fn = create_search_tools_function(mock_backend)
        raw = await fn.func("power flow", 5)
        parsed = json.loads(raw)

        assert isinstance(parsed, dict)
        assert "error" not in parsed
        assert len(parsed["results"]) == 2
        assert parsed["results"][0]["name"] == "run_opf"
        assert parsed["results"][0]["score"] == 1.5
        assert parsed["results"][1]["name"] == "build_network"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_empty_results(self):
        mock_backend = MagicMock()
        mock_backend.search = AsyncMock(return_value=[])

        fn = create_search_tools_function(mock_backend)
        raw = await fn.func("nonexistent", 5)
        parsed = json.loads(raw)
        assert parsed["results"] == []
        assert "error" not in parsed

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_backend_error_returns_json_error(self):
        mock_backend = MagicMock()
        mock_backend.search = AsyncMock(side_effect=RuntimeError("boom"))

        fn = create_search_tools_function(mock_backend)
        raw = await fn.func("test", 5)
        parsed = json.loads(raw)
        assert parsed["results"] == []
        assert "boom" in parsed["error"]
