"""Tests for the server-side search tool registered by CodeExecutionServer."""

import asyncio
import json

import pytest

from ..code_execution import CodeExecutionServer, EnvironmentConfig
from ..code_execution.auth import create_noop_auth_config
from ..code_execution.tool_registry import ToolDefinition, ToolRegistry, StateTransition


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
        """Calling search_testdomain_tools returns a valid JSON object."""
        tools = [_tool("run_opf", "Run optimal power flow")]
        server = _make_server(tools=tools)

        mcp_tool = await server.mcp.get_tool("search_testdomain_tools")
        assert mcp_tool is not None
        result = await mcp_tool.run({"query": "power flow", "top": 5})
        raw = result.content[0].text
        parsed = json.loads(raw)
        assert "results" in parsed
        assert isinstance(parsed["results"], list)

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
        assert len(parsed["results"]) >= 1
        assert parsed["results"][0]["name"] == "run_opf"

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
        assert parsed["results"] == []

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_list_domain_tools_not_registered(self):
        """list_testdomain_domain_tools is no longer registered (retired meta-tool)."""
        tools = [_tool("run_opf", "Run optimal power flow")]
        server = _make_server(tools=tools)
        tool_names = {t.name for t in await server.mcp.list_tools()}
        assert "list_testdomain_domain_tools" not in tool_names


# ---------------------------------------------------------------------------
# _setup_workflow_planning_tools
# ---------------------------------------------------------------------------


class TestSetupWorkflowPlanningTools:
    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_workflow_tools_registered_when_state_tools_exist(self):
        """plan_{name}_workflow and load_{name}_skill are registered when state-annotated tools exist."""
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
        assert "load_testdomain_skill" in tool_names

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_workflow_tools_not_registered_without_state_tools(self):
        """plan_{name}_workflow is NOT registered when no tools have state annotations."""
        tools = [_tool("run_opf", "Run OPF")]  # no state_transition
        server = _make_server(tools=tools)
        tool_names = {t.name for t in await server.mcp.list_tools()}
        assert "plan_testdomain_workflow" not in tool_names
        assert "load_testdomain_skill" not in tool_names
