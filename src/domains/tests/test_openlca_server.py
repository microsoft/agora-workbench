"""Tests for OpenLCA server config and tool registry."""

from unittest.mock import patch

import pytest
import pytest_asyncio

from tools.mcp import MCPServerDescriptor, get_mcp_registry, reset_mcp_registry
from domains.openlca.server.openlca_server import create_openlca_config
from domains.openlca.server.tool_registry import create_openlca_tool_registry

EXPECTED_TOOL_NAMES = sorted(
    [
        "run_impact_assessment",
        "list_databases",
        "list_processes",
        "create_product_system",
        "compare_scenarios",
    ]
)


@pytest_asyncio.fixture(scope="function", autouse=True)
async def setup_openlca_server():
    """Register openlca server before tests run."""
    reset_mcp_registry()
    registry = get_mcp_registry()
    with patch.object(registry, "_validate_server_connection", return_value=(True, "")):
        await registry.register(
            MCPServerDescriptor(
                name="openlca",
                url="http://localhost:8008/mcp",
                description="OpenLCA MCP Server",
                scope="https://test.scope/.default",
            )
        )
    yield
    reset_mcp_registry()


class TestOpenLCAServer:
    @pytest.mark.unit
    def test_create_openlca_config(self):
        config = create_openlca_config()
        assert config.name == "openlca"
        assert config.type == "uv"
        assert "openlca" in config.description.lower()
        assert "/app/domains/openlca/server/tools" in config.dependency_file
        assert "olca-ipc" in config.dependency_file

    @pytest.mark.unit
    def test_create_openlca_tool_registry(self):
        registry = create_openlca_tool_registry()
        names = sorted(tool.name for tool in registry.tools)
        assert names == EXPECTED_TOOL_NAMES

    @pytest.mark.unit
    def test_tool_count(self):
        registry = create_openlca_tool_registry()
        assert len(registry.tools) == 5

    @pytest.mark.unit
    def test_tool_server_name_and_module(self):
        registry = create_openlca_tool_registry()
        for tool in registry.tools:
            assert tool.server_name == "openlca"
            assert tool.module.startswith("openlca_tools.tools.")

    @pytest.mark.unit
    def test_tools_have_required_parameters(self):
        """Tools that need input should declare required_parameters."""
        registry = create_openlca_tool_registry()
        tool_map = {t.name: t for t in registry.tools}

        # run_impact_assessment needs product_system_name and impact_method
        ria = tool_map["run_impact_assessment"]
        param_names = [p.name for p in ria.required_parameters]
        assert "product_system_name" in param_names
        assert "impact_method" in param_names

        # list_databases needs no required params
        assert len(tool_map["list_databases"].required_parameters) == 0

        # create_product_system needs process_name
        cps = tool_map["create_product_system"]
        assert any(p.name == "process_name" for p in cps.required_parameters)

    @pytest.mark.unit
    def test_tools_have_return_spec(self):
        """Every tool should declare at least one return spec."""
        registry = create_openlca_tool_registry()
        for tool in registry.tools:
            assert len(tool.return_spec) >= 1
            for spec in tool.return_spec:
                assert spec.name
                assert spec.description
