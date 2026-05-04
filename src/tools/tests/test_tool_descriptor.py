"""Tests for ToolDescriptor and the framework-agnostic search descriptor factory."""

import json

import pytest

from unittest.mock import AsyncMock, MagicMock

from tools.tool_descriptor import ToolDescriptor
from tools.tool_search import ToolSearchResult
from tools.search.core import SearchToolsInput, create_search_tools_descriptor


# ---------------------------------------------------------------------------
# ToolDescriptor
# ---------------------------------------------------------------------------


class TestToolDescriptor:
    @pytest.mark.unit
    def test_fields(self):
        async def my_func(x: str) -> str:
            return x

        td = ToolDescriptor(
            name="my_tool",
            description="A test tool",
            input_schema={"type": "object", "properties": {"x": {"type": "string"}}},
            func=my_func,
        )
        assert td.name == "my_tool"
        assert td.description == "A test tool"
        assert td.input_schema["type"] == "object"
        assert td.func is my_func

    @pytest.mark.unit
    def test_input_schema_is_plain_dict(self):
        """input_schema must be a plain dict (no framework types)."""

        async def noop() -> str:
            return ""

        td = ToolDescriptor(
            name="noop",
            description="",
            input_schema={},
            func=noop,
        )
        assert isinstance(td.input_schema, dict)


# ---------------------------------------------------------------------------
# SearchToolsInput
# ---------------------------------------------------------------------------


class TestSearchToolsInput:
    @pytest.mark.unit
    def test_defaults(self):
        inp = SearchToolsInput(query="power flow")
        assert inp.query == "power flow"
        assert inp.top == 5

    @pytest.mark.unit
    def test_json_schema(self):
        schema = SearchToolsInput.model_json_schema()
        assert schema["type"] == "object"
        assert "query" in schema["properties"]
        assert "top" in schema["properties"]
        assert "query" in schema["required"]


# ---------------------------------------------------------------------------
# create_search_tools_descriptor
# ---------------------------------------------------------------------------


class TestCreateSearchToolsDescriptor:
    @pytest.mark.unit
    def test_returns_tool_descriptor(self):
        mock_backend = MagicMock()
        descriptor = create_search_tools_descriptor(mock_backend)
        assert isinstance(descriptor, ToolDescriptor)

    @pytest.mark.unit
    def test_descriptor_metadata(self):
        mock_backend = MagicMock()
        descriptor = create_search_tools_descriptor(mock_backend)
        assert descriptor.name == "search_tools"
        assert "search" in descriptor.description.lower() or "catalog" in descriptor.description.lower()

    @pytest.mark.unit
    def test_input_schema_has_query_field(self):
        mock_backend = MagicMock()
        descriptor = create_search_tools_descriptor(mock_backend)
        assert isinstance(descriptor.input_schema, dict)
        assert "query" in descriptor.input_schema.get("properties", {})

    @pytest.mark.unit
    def test_input_schema_matches_pydantic_model(self):
        """The input_schema in the descriptor must match SearchToolsInput.model_json_schema()."""
        mock_backend = MagicMock()
        descriptor = create_search_tools_descriptor(mock_backend)
        assert descriptor.input_schema == SearchToolsInput.model_json_schema()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_func_returns_json_results(self):
        results = [
            ToolSearchResult(
                name="run_opf",
                server_name="powergrid",
                description="Run OPF",
                execution_type="mcp",
                score=1.5,
            ),
        ]
        mock_backend = MagicMock()
        mock_backend.search = AsyncMock(return_value=results)

        descriptor = create_search_tools_descriptor(mock_backend)
        raw = await descriptor.func("power flow", 5)
        parsed = json.loads(raw)

        assert isinstance(parsed, dict)
        assert "error" not in parsed
        assert len(parsed["results"]) == 1
        assert parsed["results"][0]["name"] == "run_opf"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_func_handles_backend_error(self):
        mock_backend = MagicMock()
        mock_backend.search = AsyncMock(side_effect=RuntimeError("boom"))

        descriptor = create_search_tools_descriptor(mock_backend)
        raw = await descriptor.func("test", 5)
        parsed = json.loads(raw)
        assert parsed["results"] == []
        assert "boom" in parsed["error"]

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_func_empty_results(self):
        mock_backend = MagicMock()
        mock_backend.search = AsyncMock(return_value=[])

        descriptor = create_search_tools_descriptor(mock_backend)
        raw = await descriptor.func("nothing", 3)
        parsed = json.loads(raw)
        assert parsed["results"] == []
        assert "error" not in parsed
