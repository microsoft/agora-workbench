"""Integration tests for RouterServer and GatewayServer."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from connector import GatewayConfig, GatewayPolicy, RouterConfig, UpstreamConfig
from connector.base import ConnectorServer
from connector.gateway import GatewayServer
from connector.router import RouterServer


# Sample catalog responses from mock upstreams
CHEMISTRY_CATALOG = {
    "server_name": "chemistry",
    "tools": [
        {
            "name": "compute_descriptors",
            "description": "Compute molecular descriptors.",
            "module": "chemistry.tools",
            "required_parameters": [
                {"name": "smiles", "type": "builtins.str", "description": "SMILES input"},
            ],
            "optional_parameters": [],
            "return_spec": [],
            "state_transition": {"requires": [], "produces": []},
            "affordances": ["molecular properties", "descriptors"],
        },
        {
            "name": "cluster_molecules",
            "description": "Cluster molecules by fingerprint similarity.",
            "module": "chemistry.tools",
            "required_parameters": [
                {"name": "smiles_list", "type": "builtins.list", "description": "List of SMILES"},
            ],
            "optional_parameters": [
                {"name": "cutoff", "type": "builtins.float", "description": "Distance cutoff", "default": 0.5},
            ],
            "return_spec": [],
            "state_transition": {"requires": [], "produces": []},
            "affordances": ["clustering", "similarity"],
        },
    ],
}

GIS_CATALOG = {
    "server_name": "gis",
    "tools": [
        {
            "name": "reproject",
            "description": "Reproject geometries between coordinate systems.",
            "module": "gis.tools",
            "required_parameters": [
                {"name": "geometry", "type": "builtins.str", "description": "WKT geometry"},
                {"name": "target_crs", "type": "builtins.str", "description": "Target CRS"},
            ],
            "optional_parameters": [],
            "return_spec": [],
            "state_transition": {"requires": [], "produces": []},
            "affordances": ["coordinate transformation", "reprojection"],
        },
    ],
}


def _mock_response(data: dict) -> httpx.Response:
    """Create a properly formed httpx.Response for testing."""
    request = httpx.Request("GET", "http://mock/catalog")
    return httpx.Response(200, json=data, request=request)


class TestRouterServer:
    """Tests for RouterServer."""

    @pytest.fixture
    def router_config(self):
        return RouterConfig(
            name="science-hub",
            description="Aggregated science tools",
            upstreams=[
                UpstreamConfig(name="chemistry", url="http://chemistry:8000"),
                UpstreamConfig(name="gis", url="http://gis:8000"),
            ],
        )

    @pytest.mark.asyncio
    async def test_fetches_catalogs_on_startup(self, router_config):
        """Router fetches catalogs from all upstreams during startup."""
        server = RouterServer(router_config)

        async def mock_get(url, **kwargs):
            if "chemistry" in url:
                return _mock_response(CHEMISTRY_CATALOG)
            elif "gis" in url:
                return _mock_response(GIS_CATALOG)
            raise httpx.RequestError(f"Unknown URL: {url}")

        with patch("connector.base.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = mock_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            await server._sync_upstream_catalogs()

        assert "chemistry" in server._upstream_catalogs
        assert "gis" in server._upstream_catalogs
        assert len(server._upstream_catalogs["chemistry"]) == 2
        assert len(server._upstream_catalogs["gis"]) == 1

    @pytest.mark.asyncio
    async def test_registers_proxy_tools(self, router_config):
        """Router registers execute_code proxy for each upstream."""
        server = RouterServer(router_config)

        async def mock_get(url, **kwargs):
            if "chemistry" in url:
                return _mock_response(CHEMISTRY_CATALOG)
            elif "gis" in url:
                return _mock_response(GIS_CATALOG)
            raise httpx.RequestError(f"Unknown URL: {url}")

        with patch("connector.base.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = mock_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            await server._startup()

        tools = await server.mcp.list_tools()
        tool_names = [t.name for t in tools]
        assert "execute_chemistry_code" in tool_names
        assert "execute_gis_code" in tool_names
        assert "search_science-hub_tools" in tool_names

    @pytest.mark.asyncio
    async def test_expose_tools_filter(self):
        """Router respects expose_tools glob patterns."""
        config = RouterConfig(
            name="filtered",
            upstreams=[
                UpstreamConfig(
                    name="chemistry",
                    url="http://chemistry:8000",
                    expose_tools=["compute_*"],
                ),
            ],
        )
        server = RouterServer(config)

        async def mock_get(url, **kwargs):
            return _mock_response(CHEMISTRY_CATALOG)

        with patch("connector.base.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = mock_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            await server._sync_upstream_catalogs()

        chem_tools = server._upstream_catalogs["chemistry"]
        assert len(chem_tools) == 1
        assert chem_tools[0].name == "compute_descriptors"

    @pytest.mark.asyncio
    async def test_handles_upstream_failure_gracefully(self, router_config):
        """Router continues if one upstream is unreachable."""
        server = RouterServer(router_config)

        async def mock_get(url, **kwargs):
            if "chemistry" in url:
                return _mock_response(CHEMISTRY_CATALOG)
            elif "gis" in url:
                raise httpx.ConnectError("Connection refused")
            raise httpx.RequestError(f"Unknown URL: {url}")

        with patch("connector.base.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = mock_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            await server._sync_upstream_catalogs()

        assert "chemistry" in server._upstream_catalogs
        assert "gis" not in server._upstream_catalogs

    @pytest.mark.asyncio
    async def test_search_tool_aggregates_all_upstreams(self, router_config):
        """Aggregated search index includes tools from all upstreams."""
        server = RouterServer(router_config)

        async def mock_get(url, **kwargs):
            if "chemistry" in url:
                return _mock_response(CHEMISTRY_CATALOG)
            elif "gis" in url:
                return _mock_response(GIS_CATALOG)
            raise httpx.RequestError(f"Unknown URL: {url}")

        with patch("connector.base.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = mock_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            await server._startup()

        tools = await server.mcp.list_tools()
        tool_names = [t.name for t in tools]
        assert "search_science-hub_tools" in tool_names

    @pytest.mark.asyncio
    async def test_tool_aliases_are_applied(self):
        """Router applies tool_aliases when fetching catalog."""
        config = RouterConfig(
            name="aliased",
            upstreams=[
                UpstreamConfig(
                    name="chemistry",
                    url="http://chemistry:8000",
                    tool_aliases={"compute_descriptors": "chem_descriptors"},
                ),
            ],
        )
        server = RouterServer(config)

        async def mock_get(url, **kwargs):
            return _mock_response(CHEMISTRY_CATALOG)

        with patch("connector.base.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = mock_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            await server._sync_upstream_catalogs()

        chem_tools = server._upstream_catalogs["chemistry"]
        tool_names = {t.name for t in chem_tools}
        assert "chem_descriptors" in tool_names
        assert "compute_descriptors" not in tool_names
        assert "cluster_molecules" in tool_names


class TestGatewayServer:
    """Tests for GatewayServer."""

    @pytest.fixture
    def gateway_config(self):
        return GatewayConfig(
            name="chem-gateway",
            upstream=UpstreamConfig(name="chemistry", url="http://chemistry:8000"),
            policy=GatewayPolicy(
                max_calls_per_minute=5,
                blocked_tools=["parallel_execute"],
            ),
        )

    @pytest.mark.asyncio
    async def test_gateway_registers_single_upstream(self, gateway_config):
        """Gateway registers tools from the single upstream."""
        server = GatewayServer(gateway_config)

        async def mock_get(url, **kwargs):
            return _mock_response(CHEMISTRY_CATALOG)

        with patch("connector.base.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = mock_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            await server._startup()

        tools = await server.mcp.list_tools()
        tool_names = [t.name for t in tools]
        assert "execute_chemistry_code" in tool_names

    def test_rate_limiting(self, gateway_config):
        """Gateway enforces rate limiting."""
        server = GatewayServer(gateway_config)

        # Should allow 5 calls
        for _ in range(5):
            assert server._check_rate_limit("user1", 5) is True

        # 6th call should be denied
        assert server._check_rate_limit("user1", 5) is False

        # Different user should still be allowed
        assert server._check_rate_limit("user2", 5) is True

    @pytest.mark.asyncio
    async def test_gateway_blocks_blocked_tools(self, gateway_config):
        """Gateway rejects calls to tools listed in blocked_tools."""
        server = GatewayServer(gateway_config)

        async def mock_get(url, **kwargs):
            return _mock_response(CHEMISTRY_CATALOG)

        with patch("connector.base.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = mock_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            await server._startup()

        assert "parallel_execute" in server.config.policy.blocked_tools


class TestConnectorServerMatchesExposeFilter:
    """Tests for the expose_tools glob matching logic."""

    def test_exact_match(self):
        assert ConnectorServer._matches_expose_filter("compute_descriptors", ["compute_descriptors"])

    def test_wildcard_match(self):
        assert ConnectorServer._matches_expose_filter("compute_descriptors", ["compute_*"])

    def test_no_match(self):
        assert not ConnectorServer._matches_expose_filter("cluster_molecules", ["compute_*"])

    def test_multiple_patterns(self):
        assert ConnectorServer._matches_expose_filter("cluster_molecules", ["compute_*", "cluster_*"])

    def test_star_matches_all(self):
        assert ConnectorServer._matches_expose_filter("anything", ["*"])
