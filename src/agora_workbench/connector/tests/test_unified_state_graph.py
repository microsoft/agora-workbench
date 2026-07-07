"""Tests for the unified state graph on RouterServer."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from agora_workbench.code_execution.tools.search.state_graph import StateGraph
from agora_workbench.code_execution.tools.tool_search import ToolInfo
from agora_workbench.connector import BridgeEdge, RouterConfig, UpstreamConfig
from agora_workbench.connector.router import RouterServer


# Catalogs with state-annotated tools for two domains
GRAPHORMER_CATALOG = {
    "server_name": "graphormer",
    "tools": [
        {
            "name": "predict_reduction_potential",
            "description": "Predict reduction potential from SMILES.",
            "module": "graphormer.tools",
            "required_parameters": [
                {"name": "smiles", "type": "builtins.str", "description": "SMILES string"},
            ],
            "optional_parameters": [],
            "return_spec": [],
            "state_transition": {
                "requires": ["graphormer.molecule_loaded"],
                "produces": ["graphormer.reduction_predicted"],
            },
            "affordances": ["electrochemistry", "reduction potential"],
        },
        {
            "name": "load_molecule",
            "description": "Load a molecule from SMILES.",
            "module": "graphormer.tools",
            "required_parameters": [
                {"name": "smiles", "type": "builtins.str", "description": "SMILES string"},
            ],
            "optional_parameters": [],
            "return_spec": [],
            "state_transition": {
                "requires": [],
                "produces": ["graphormer.molecule_loaded"],
            },
            "affordances": ["molecule loading"],
        },
    ],
    "skills": [],
}

EZBATTERY_CATALOG = {
    "server_name": "ezbattery",
    "tools": [
        {
            "name": "simulate_negolyte",
            "description": "Simulate negolyte performance.",
            "module": "ezbattery.tools",
            "required_parameters": [
                {"name": "potential", "type": "builtins.float", "description": "Reduction potential"},
            ],
            "optional_parameters": [],
            "return_spec": [],
            "state_transition": {
                "requires": ["ezbattery.electrolyte_configured"],
                "produces": ["ezbattery.simulation_complete"],
            },
            "affordances": ["battery simulation", "negolyte"],
        },
        {
            "name": "configure_electrolyte",
            "description": "Configure electrolyte parameters.",
            "module": "ezbattery.tools",
            "required_parameters": [],
            "optional_parameters": [],
            "return_spec": [],
            "state_transition": {
                "requires": [],
                "produces": ["ezbattery.electrolyte_configured"],
            },
            "affordances": ["electrolyte setup"],
        },
    ],
    "skills": [
        {
            "name": "battery_workflow",
            "description": "End-to-end battery simulation workflow.",
            "domain": "ezbattery",
            "states": ["ezbattery.electrolyte_configured", "ezbattery.simulation_complete"],
        },
    ],
}


def _mock_response(data: dict) -> httpx.Response:
    request = httpx.Request("GET", "http://mock/catalog")
    return httpx.Response(200, json=data, request=request)


# ============================================================================
# StateGraph.inject_bridges unit tests
# ============================================================================


class TestInjectBridges:
    """Unit tests for StateGraph.inject_bridges."""

    def test_inject_adds_synthetic_tool(self):
        """Bridge injection creates a synthetic ToolInfo in the adjacency."""
        tools = [
            ToolInfo(
                name="predict_reduction_potential",
                description="Predict reduction potential.",
                server_name="graphormer",
                state_requires=("graphormer.molecule_loaded",),
                state_produces=("graphormer.reduction_predicted",),
            ),
            ToolInfo(
                name="simulate_negolyte",
                description="Simulate negolyte.",
                server_name="ezbattery",
                state_requires=("ezbattery.electrolyte_configured",),
                state_produces=("ezbattery.simulation_complete",),
            ),
        ]
        graph = StateGraph(tools=tools, skills=[])

        # Before bridge: no path between domains
        path = graph.path("graphormer.reduction_predicted", "ezbattery.simulation_complete")
        assert path.get("path") is None

        # Inject bridge
        graph.inject_bridges([
            {
                "from_state": "graphormer.reduction_predicted",
                "to_state": "ezbattery.electrolyte_configured",
                "description": "Pass predicted potentials to battery sim",
            }
        ])

        # After bridge: path exists
        path = graph.path("graphormer.reduction_predicted", "ezbattery.simulation_complete")
        assert path["path"] is not None
        assert len(path["path"]) == 2  # bridge + simulate_negolyte

        # First step is the bridge
        bridge_step = path["path"][0]
        assert bridge_step["server"] == "(bridge)"
        assert "bridge:" in bridge_step["tool"]
        assert bridge_step["from_state"] == "graphormer.reduction_predicted"
        assert bridge_step["to_state"] == "ezbattery.electrolyte_configured"

    def test_inject_shows_in_from_state(self):
        """Bridge appears in from_state query results."""
        tools = [
            ToolInfo(
                name="predict_reduction_potential",
                description="Predict reduction potential.",
                server_name="graphormer",
                state_requires=("graphormer.molecule_loaded",),
                state_produces=("graphormer.reduction_predicted",),
            ),
        ]
        graph = StateGraph(tools=tools, skills=[])
        graph.inject_bridges([
            {
                "from_state": "graphormer.reduction_predicted",
                "to_state": "ezbattery.electrolyte_configured",
            }
        ])

        result = graph.from_state("graphormer.reduction_predicted")
        tool_names = [t["name"] for t in result["tools_from_here"]]
        assert any("bridge:" in name for name in tool_names)
        assert "ezbattery.electrolyte_configured" in result["next_states"]

    def test_inject_updates_domain_vocabulary(self):
        """Bridge injection adds missing states to domain vocabulary."""
        tools = [
            ToolInfo(
                name="tool_a",
                description="Tool A",
                server_name="alpha",
                state_requires=(),
                state_produces=("alpha.done",),
            ),
        ]
        graph = StateGraph(tools=tools, skills=[])
        graph.inject_bridges([
            {"from_state": "alpha.done", "to_state": "beta.ready"}
        ])

        # beta domain should now exist
        assert "beta" in graph._domain_states
        assert "beta.ready" in graph._domain_states["beta"]

    def test_overview_includes_bridge_edges(self):
        """Overview mode shows bridge edges in the graph."""
        tools = [
            ToolInfo(
                name="predict_reduction_potential",
                description="Predict.",
                server_name="graphormer",
                state_requires=("graphormer.molecule_loaded",),
                state_produces=("graphormer.reduction_predicted",),
            ),
        ]
        graph = StateGraph(tools=tools, skills=[])
        graph.inject_bridges([
            {
                "from_state": "graphormer.reduction_predicted",
                "to_state": "ezbattery.electrolyte_configured",
            }
        ])

        overview = graph.overview()
        # The bridge tool's server is "(bridge)", so it won't appear in a
        # single-domain overview filter. But in the full overview, the
        # target state should exist in its domain.
        domains = {d["domain"] for d in overview["domains"]}
        assert "ezbattery" in domains


# ============================================================================
# RouterServer unified state graph integration tests
# ============================================================================


class TestRouterUnifiedStateGraph:
    """Integration tests for the router's unified state graph."""

    @pytest.fixture
    def router_config_with_bridges(self):
        return RouterConfig(
            name="science-hub",
            description="Aggregated science tools",
            upstreams=[
                UpstreamConfig(name="graphormer", url="http://graphormer:8000"),
                UpstreamConfig(name="ezbattery", url="http://ezbattery:8000"),
            ],
            bridges=[
                BridgeEdge(
                    from_state="graphormer.reduction_predicted",
                    to_state="ezbattery.electrolyte_configured",
                    description="Pass predicted potentials to battery simulation",
                ),
            ],
        )

    @pytest.fixture
    def router_config_no_bridges(self):
        return RouterConfig(
            name="science-hub",
            description="Aggregated science tools",
            upstreams=[
                UpstreamConfig(name="graphormer", url="http://graphormer:8000"),
                UpstreamConfig(name="ezbattery", url="http://ezbattery:8000"),
            ],
        )

    async def _start_router(self, config):
        """Helper to start a router with mocked upstream catalogs."""
        server = RouterServer(config)

        async def mock_get(url, **kwargs):
            if "graphormer" in url:
                return _mock_response(GRAPHORMER_CATALOG)
            elif "ezbattery" in url:
                return _mock_response(EZBATTERY_CATALOG)
            raise httpx.RequestError(f"Unknown URL: {url}")

        with patch("agora_workbench.connector.base.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = mock_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            await server._startup()

        return server

    @pytest.mark.asyncio
    async def test_registers_unified_plan_tool(self, router_config_with_bridges):
        """Router registers plan_{name}_workflow when bridges are configured."""
        server = await self._start_router(router_config_with_bridges)

        tools = await server.mcp.list_tools()
        tool_names = [t.name for t in tools]
        assert "plan_science-hub_workflow" in tool_names

    @pytest.mark.asyncio
    async def test_registers_unified_plan_tool_without_bridges(self, router_config_no_bridges):
        """Router registers plan tool even without bridges if state-annotated tools exist."""
        server = await self._start_router(router_config_no_bridges)

        tools = await server.mcp.list_tools()
        tool_names = [t.name for t in tools]
        # Should still register because there are state-annotated tools
        assert "plan_science-hub_workflow" in tool_names

    @pytest.mark.asyncio
    async def test_invalid_bridge_fails_loudly(self):
        """Router startup fails if a bridge references a non-existent state."""
        config = RouterConfig(
            name="bad-hub",
            upstreams=[
                UpstreamConfig(name="graphormer", url="http://graphormer:8000"),
            ],
            bridges=[
                BridgeEdge(
                    from_state="graphormer.reduction_predicted",
                    to_state="nonexistent.fake_state",
                    description="Invalid bridge",
                ),
            ],
        )

        server = RouterServer(config)

        async def mock_get(url, **kwargs):
            return _mock_response(GRAPHORMER_CATALOG)

        with patch("agora_workbench.connector.base.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = mock_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with pytest.raises(ValueError, match="Invalid bridge edge"):
                await server._startup()

    @pytest.mark.asyncio
    async def test_cross_server_path_via_bridge(self, router_config_with_bridges):
        """Unified graph can find cross-server paths via bridge edges."""
        server = await self._start_router(router_config_with_bridges)

        # Call the registered tool function directly via the MCP tool list
        tools = await server.mcp.list_tools()
        plan_tool = next(t for t in tools if t.name == "plan_science-hub_workflow")
        assert plan_tool is not None

        # We can't easily call the tool directly in tests without a full MCP
        # context, so test the underlying graph via _setup_unified_state_graph
        # by rebuilding it. The integration test above verifies registration.
        # For path testing, use the StateGraph directly.
        all_tool_infos = []
        for upstream_name, catalog_tools in server._upstream_catalogs.items():
            for td in catalog_tools:
                if td.state_transition.requires or td.state_transition.produces:
                    all_tool_infos.append(
                        ToolInfo(
                            name=td.name,
                            description=td.description,
                            server_name=td.server_name or upstream_name,
                            state_requires=tuple(sorted(td.state_transition.requires)),
                            state_produces=tuple(sorted(td.state_transition.produces)),
                        )
                    )

        graph = StateGraph(tools=all_tool_infos, skills=[])
        graph.inject_bridges([b.model_dump() for b in router_config_with_bridges.bridges])

        # Path from graphormer output to ezbattery output should cross the bridge
        result = graph.path("graphormer.reduction_predicted", "ezbattery.simulation_complete")
        assert result["path"] is not None
        servers_in_path = [step["server"] for step in result["path"]]
        assert "(bridge)" in servers_in_path
        assert "ezbattery" in servers_in_path
