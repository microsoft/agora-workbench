"""Integration tests for ConnectorServer (router and gateway modes)."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from code_execution.connector import ConnectorConfig, ConnectorServer, GatewayPolicy, UpstreamConfig


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


class TestConnectorServerRouter:
    """Tests for ConnectorServer in router mode."""

    @pytest.fixture
    def router_config(self):
        return ConnectorConfig(
            name="science-hub",
            mode="router",
            description="Aggregated science tools",
            upstreams=[
                UpstreamConfig(name="chemistry", url="http://chemistry:8000"),
                UpstreamConfig(name="gis", url="http://gis:8000"),
            ],
        )

    @pytest.mark.asyncio
    async def test_fetches_catalogs_on_startup(self, router_config):
        """Router fetches catalogs from all upstreams during startup."""
        connector = ConnectorServer(config=router_config)

        async def mock_get(url, **kwargs):
            if "chemistry" in url:
                return _mock_response(CHEMISTRY_CATALOG)
            elif "gis" in url:
                return _mock_response(GIS_CATALOG)
            raise httpx.RequestError(f"Unknown URL: {url}")

        with patch("code_execution.connector.server.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = mock_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            await connector._sync_upstream_catalogs()

        assert "chemistry" in connector._upstream_catalogs
        assert "gis" in connector._upstream_catalogs
        assert len(connector._upstream_catalogs["chemistry"]) == 2
        assert len(connector._upstream_catalogs["gis"]) == 1

    @pytest.mark.asyncio
    async def test_registers_proxy_tools(self, router_config):
        """Router registers execute_code proxy for each upstream."""
        connector = ConnectorServer(config=router_config)

        async def mock_get(url, **kwargs):
            if "chemistry" in url:
                return _mock_response(CHEMISTRY_CATALOG)
            elif "gis" in url:
                return _mock_response(GIS_CATALOG)
            raise httpx.RequestError(f"Unknown URL: {url}")

        with patch("code_execution.connector.server.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = mock_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            await connector._startup()

        # Check that execute_code proxies are registered
        tools = await connector.mcp.list_tools()
        tool_names = [t.name for t in tools]
        assert "execute_chemistry_code" in tool_names
        assert "execute_gis_code" in tool_names
        # Search tool should be registered
        assert "search_science-hub_tools" in tool_names

    @pytest.mark.asyncio
    async def test_expose_tools_filter(self):
        """Router respects expose_tools glob patterns."""
        config = ConnectorConfig(
            name="filtered",
            mode="router",
            upstreams=[
                UpstreamConfig(
                    name="chemistry",
                    url="http://chemistry:8000",
                    expose_tools=["compute_*"],
                ),
            ],
        )
        connector = ConnectorServer(config=config)

        async def mock_get(url, **kwargs):
            return _mock_response(CHEMISTRY_CATALOG)

        with patch("code_execution.connector.server.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = mock_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            await connector._sync_upstream_catalogs()

        # Only compute_descriptors should pass the filter
        chem_tools = connector._upstream_catalogs["chemistry"]
        assert len(chem_tools) == 1
        assert chem_tools[0].name == "compute_descriptors"

    @pytest.mark.asyncio
    async def test_handles_upstream_failure_gracefully(self, router_config):
        """Router continues if one upstream is unreachable."""
        connector = ConnectorServer(config=router_config)

        async def mock_get(url, **kwargs):
            if "chemistry" in url:
                return _mock_response(CHEMISTRY_CATALOG)
            elif "gis" in url:
                raise httpx.ConnectError("Connection refused")
            raise httpx.RequestError(f"Unknown URL: {url}")

        with patch("code_execution.connector.server.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = mock_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            await connector._sync_upstream_catalogs()

        # Chemistry should succeed, gis should be absent
        assert "chemistry" in connector._upstream_catalogs
        assert "gis" not in connector._upstream_catalogs

    @pytest.mark.asyncio
    async def test_search_tool_aggregates_all_upstreams(self, router_config):
        """Aggregated search index includes tools from all upstreams."""
        connector = ConnectorServer(config=router_config)

        async def mock_get(url, **kwargs):
            if "chemistry" in url:
                return _mock_response(CHEMISTRY_CATALOG)
            elif "gis" in url:
                return _mock_response(GIS_CATALOG)
            raise httpx.RequestError(f"Unknown URL: {url}")

        with patch("code_execution.connector.server.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = mock_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            await connector._startup()

        # Check search tool is registered
        tools = await connector.mcp.list_tools()
        tool_names = [t.name for t in tools]
        assert "search_science-hub_tools" in tool_names


class TestConnectorServerGateway:
    """Tests for ConnectorServer in gateway mode."""

    @pytest.fixture
    def gateway_config(self):
        return ConnectorConfig(
            name="chem-gateway",
            mode="gateway",
            upstreams=[
                UpstreamConfig(name="chemistry", url="http://chemistry:8000"),
            ],
            gateway_policy=GatewayPolicy(
                max_calls_per_minute=5,
                blocked_tools=["parallel_execute"],
            ),
        )

    @pytest.mark.asyncio
    async def test_gateway_registers_single_upstream(self, gateway_config):
        """Gateway registers tools from the single upstream."""
        connector = ConnectorServer(config=gateway_config)

        async def mock_get(url, **kwargs):
            return _mock_response(CHEMISTRY_CATALOG)

        with patch("code_execution.connector.server.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = mock_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            await connector._startup()

        tools = await connector.mcp.list_tools()
        tool_names = [t.name for t in tools]
        assert "execute_chemistry_code" in tool_names

    def test_rate_limiting(self, gateway_config):
        """Gateway enforces rate limiting."""
        connector = ConnectorServer(config=gateway_config)

        # Should allow 5 calls
        for _ in range(5):
            assert connector._check_rate_limit("user1", 5) is True

        # 6th call should be denied
        assert connector._check_rate_limit("user1", 5) is False

        # Different user should still be allowed
        assert connector._check_rate_limit("user2", 5) is True


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
