"""Tests for the server-side search tool registered by CodeExecutionServer."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

import code_execution.tools as code_execution_tools

from .. import CodeExecutionServer, EnvironmentConfig
from ..auth import create_noop_auth_config
from ..tool_registry import ToolDefinition, ToolRegistry, StateTransition


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_server(tools: list[ToolDefinition] | None = None) -> CodeExecutionServer:
    """Create a minimal CodeExecutionServer for unit tests."""
    config = EnvironmentConfig(
        name="testdomain",
        type="uv",
        description="Test environment",
        dependency_file="# empty",
    )
    registry: ToolRegistry | None = None
    if tools is not None:
        registry = ToolRegistry()
        for t in tools:
            registry.register_tool(t)
    return CodeExecutionServer(
        environment_config=config,
        tool_registry=registry,
        auth_config=create_noop_auth_config(),
    )


def _tool(name: str, description: str = "", **kwargs) -> ToolDefinition:
    """Quick ToolDefinition factory."""
    return ToolDefinition(
        name=name,
        description=description or f"Tool {name}",
        module="test.module",
        **kwargs,
    )


def _registered_tool_names(server: CodeExecutionServer) -> set[str]:
    """Return the names of all registered MCP tools on the server."""
    return {t.name for t in asyncio.get_event_loop().run_until_complete(server.mcp.list_tools())}


async def _get_mcp_tool(server: CodeExecutionServer, name: str):
    """Return the FastMCP tool object for *name*."""
    return await server.mcp.get_tool(name)


# ---------------------------------------------------------------------------
# _build_tool_infos
# ---------------------------------------------------------------------------


class TestBuildToolInfos:
    @pytest.mark.unit
    def test_empty_registry_returns_empty(self):
        server = _make_server(tools=[])
        infos = server._build_tool_infos()
        assert infos == []

    @pytest.mark.unit
    def test_no_registry_returns_empty(self):
        server = _make_server(tools=None)
        infos = server._build_tool_infos()
        assert infos == []

    @pytest.mark.unit
    def test_basic_tool_info_fields(self):
        tool = _tool("run_opf", "Run optimal power flow")
        server = _make_server(tools=[tool])
        infos = server._build_tool_infos()
        assert len(infos) == 1
        assert infos[0].name == "run_opf"
        assert infos[0].description == "Run optimal power flow"
        assert infos[0].server_name == "testdomain"
        assert infos[0].affordances == ()
        assert infos[0].state_requires == ()
        assert infos[0].state_produces == ()

    @pytest.mark.unit
    def test_state_transition_propagated(self):
        tool = _tool(
            "solve_flowsheet",
            "Solve a flowsheet",
            state_transition=StateTransition(
                requires=frozenset({"sim.flowsheet_loaded"}),
                produces=frozenset({"sim.flowsheet_solved"}),
            ),
        )
        server = _make_server(tools=[tool])
        infos = server._build_tool_infos()
        assert len(infos) == 1
        assert infos[0].state_requires == ("sim.flowsheet_loaded",)
        assert infos[0].state_produces == ("sim.flowsheet_solved",)

    @pytest.mark.unit
    def test_tool_affordances_propagated(self):
        tool = _tool("fast_opf", "Fast OPF", affordances=["optimal dispatch", "power flow"])
        server = _make_server(tools=[tool])
        infos = server._build_tool_infos()
        assert "optimal dispatch" in infos[0].affordances
        assert "power flow" in infos[0].affordances

    @pytest.mark.unit
    def test_multiple_tools(self):
        tools = [_tool("tool_a", "First"), _tool("tool_b", "Second")]
        server = _make_server(tools=tools)
        infos = server._build_tool_infos()
        assert len(infos) == 2
        names = {i.name for i in infos}
        assert names == {"tool_a", "tool_b"}


# ---------------------------------------------------------------------------
# _setup_search_tool — verify the MCP tool is registered
# ---------------------------------------------------------------------------


class TestSetupSearchTool:
    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_search_tool_registered(self):
        """search_testdomain_tools is registered in the MCP server."""
        tools = [_tool("run_opf", "Run optimal power flow")]
        server = _make_server(tools=tools)
        tool_names = {t.name for t in await server.mcp.list_tools()}
        assert "search_testdomain_tools" in tool_names

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_no_search_tool_without_registry(self):
        """search_testdomain_tools is NOT registered when there is no tool_registry."""
        server = _make_server(tools=None)
        tool_names = {t.name for t in await server.mcp.list_tools()}
        assert "search_testdomain_tools" not in tool_names

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_search_tool_returns_json(self):
        """Calling search_testdomain_tools returns a valid JSON object with grouped results."""
        tools = [_tool("run_opf", "Run optimal power flow")]
        server = _make_server(tools=tools)

        mcp_tool = await server.mcp.get_tool("search_testdomain_tools")
        assert mcp_tool is not None
        result = await mcp_tool.run({"query": "power flow", "top": 5})
        raw = result.content[0].text
        parsed = json.loads(raw)
        assert "tools" in parsed
        assert "skills" in parsed
        assert isinstance(parsed["tools"], list)
        assert isinstance(parsed["skills"], list)

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_search_finds_tool_by_name(self):
        """search_testdomain_tools can find a tool by name."""
        tools = [
            _tool("run_opf", "Run optimal power flow"),
            _tool("build_network", "Build a network topology"),
        ]
        server = _make_server(tools=tools)

        mcp_tool = await server.mcp.get_tool("search_testdomain_tools")
        result = await mcp_tool.run({"query": "run_opf", "top": 5})
        raw = result.content[0].text
        parsed = json.loads(raw)
        assert len(parsed["tools"]) >= 1
        assert parsed["tools"][0]["name"] == "run_opf"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_search_empty_results_on_no_match(self):
        """Empty results returned when query matches nothing."""
        tools = [_tool("run_opf", "Run optimal power flow")]
        server = _make_server(tools=tools)

        mcp_tool = await server.mcp.get_tool("search_testdomain_tools")
        result = await mcp_tool.run({"query": "zzz_no_match_xyz", "top": 5})
        raw = result.content[0].text
        parsed = json.loads(raw)
        assert parsed["tools"] == []
        assert parsed["skills"] == []

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_list_domain_tools_not_registered(self):
        """list_{name}_domain_tools is not registered (superseded by search tool)."""
        tools = [_tool("run_opf", "Run optimal power flow")]
        server = _make_server(tools=tools)
        tool_names = {t.name for t in await server.mcp.list_tools()}
        assert "list_testdomain_domain_tools" not in tool_names

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_search_tool_accepts_category_param(self):
        """search_testdomain_tools accepts the category parameter."""
        tools = [_tool("run_opf", "Run optimal power flow")]
        server = _make_server(tools=tools)

        mcp_tool = await server.mcp.get_tool("search_testdomain_tools")
        result = await mcp_tool.run({"query": "power flow", "top": 5, "category": "tools"})
        raw = result.content[0].text
        parsed = json.loads(raw)
        assert "tools" in parsed
        assert "skills" in parsed

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_search_tool_invalid_category_returns_error(self):
        """Invalid category value returns a structured error."""
        tools = [_tool("run_opf", "Run optimal power flow")]
        server = _make_server(tools=tools)

        mcp_tool = await server.mcp.get_tool("search_testdomain_tools")
        result = await mcp_tool.run({"query": "power", "top": 5, "category": "invalid"})
        raw = result.content[0].text
        parsed = json.loads(raw)
        assert "error" in parsed

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_search_tool_result_has_type_field(self):
        """Tool results include a type field set to 'tool'."""
        tools = [_tool("run_opf", "Run optimal power flow")]
        server = _make_server(tools=tools)

        mcp_tool = await server.mcp.get_tool("search_testdomain_tools")
        result = await mcp_tool.run({"query": "power flow", "top": 5})
        raw = result.content[0].text
        parsed = json.loads(raw)
        assert len(parsed["tools"]) >= 1
        assert parsed["tools"][0]["type"] == "tool"
        assert "to_access" in parsed["tools"][0]


# ---------------------------------------------------------------------------
# _setup_workflow_planning_tools
# ---------------------------------------------------------------------------


class _AsyncSearchBackend:
    def __init__(self):
        self.initialized = False
        self.closed = False

    def index(self, tools, skills=None, server_name=""):
        pass

    async def initialize(self):
        self.initialized = True

    async def search(self, query: str, top: int = 5):
        return []

    async def close(self):
        self.closed = True


class TestServerSearchBackendLifecycle:
    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_async_search_backend_initialized_on_startup(self, monkeypatch: pytest.MonkeyPatch):
        backend = _AsyncSearchBackend()
        monkeypatch.setattr(code_execution_tools, "create_tool_search_backend", lambda *args, **kwargs: backend)

        server = _make_server(tools=[_tool("run_opf", "Run optimal power flow")])
        server._ensure_environment = AsyncMock()
        server._register_kernel = AsyncMock()

        await server._startup()
        assert backend.initialized is True

        await server._shutdown()
        assert backend.closed is True


class TestSetupWorkflowPlanningTools:
    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_plan_workflow_registered_when_state_tools_exist(self):
        """plan_{name}_workflow is registered when state-annotated tools exist."""
        tool = _tool(
            "solve",
            "Solve something",
            state_transition=StateTransition(
                requires=frozenset({"sim.ready"}),
                produces=frozenset({"sim.solved"}),
            ),
        )
        server = _make_server(tools=[tool])
        tool_names = {t.name for t in await server.mcp.list_tools()}
        assert "plan_testdomain_workflow" in tool_names

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_load_skill_not_registered_without_skills(self):
        """load_{name}_skill is NOT registered when no skills are discoverable."""
        tool = _tool(
            "solve",
            "Solve something",
            state_transition=StateTransition(
                requires=frozenset({"sim.ready"}),
                produces=frozenset({"sim.solved"}),
            ),
        )
        server = _make_server(tools=[tool])
        tool_names = {t.name for t in await server.mcp.list_tools()}
        # No domains_dir configured → no skills → load_skill not registered
        assert "load_testdomain_skill" not in tool_names

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_workflow_tools_not_registered_without_state_tools(self):
        """plan_{name}_workflow is NOT registered when no tools have state annotations."""
        tools = [_tool("run_opf", "Run OPF")]  # no state_transition
        server = _make_server(tools=tools)
        tool_names = {t.name for t in await server.mcp.list_tools()}
        assert "plan_testdomain_workflow" not in tool_names
        # load_skill may or may not be registered depending on skill discovery
        # (no domains_dir configured in test → no skills → not registered)
