"""Tests for DWSIM server config and tool registry."""

from unittest.mock import patch

import pytest
import pytest_asyncio

from tools.mcp import MCPServerDescriptor, get_mcp_registry, reset_mcp_registry
from domains.dwsim.server.dwsim_server import create_dwsim_config
from domains.dwsim.server.tool_registry import create_dwsim_tool_registry

# Core tools that must always be present (subset, not exhaustive).
# New tools may be added without breaking this test.
REQUIRED_TOOL_NAMES = {
    # Flowsheet lifecycle
    "search_compounds",
    "create_flowsheet",
    "load_flowsheet",
    "solve_flowsheet",
    # Streams
    "add_material_stream",
    "add_energy_stream",
    # Unit operations (representative subset)
    "add_heater",
    "add_separator",
    "add_distillation_column",
    "add_conversion_reactor",
    # Results
    "get_stream_results",
    "get_unit_operation_results",
    "get_flowsheet_summary",
    # Optimisation
    "run_sensitivity_analysis",
    "run_optimization",
}


@pytest_asyncio.fixture(scope="function", autouse=True)
async def setup_dwsim_server():
    """Register dwsim server before tests run."""
    reset_mcp_registry()
    registry = get_mcp_registry()
    with patch.object(registry, "_validate_server_connection", return_value=(True, "")):
        await registry.register(
            MCPServerDescriptor(
                name="dwsim",
                url="http://localhost:8004/mcp",
                description="DWSIM MCP Server",
                scope="https://test.scope/.default",
            )
        )
    yield
    reset_mcp_registry()


class TestDWSIMServer:
    @pytest.mark.unit
    def test_create_dwsim_config(self):
        config = create_dwsim_config()
        assert config.name == "dwsim"
        assert config.type == "uv"
        assert "dwsim" in config.description.lower()
        assert "pythonnet==3.0.5" in config.dependency_file
        assert "/app/domains/dwsim/server/tools" in config.dependency_file

    @pytest.mark.unit
    def test_create_dwsim_tool_registry(self):
        registry = create_dwsim_tool_registry()
        names = {tool.name for tool in registry.tools}
        missing = REQUIRED_TOOL_NAMES - names
        assert not missing, f"Missing required tools: {missing}"
        # Sanity-check: registry should have a reasonable number of tools
        assert len(names) >= len(REQUIRED_TOOL_NAMES)

    @pytest.mark.unit
    def test_tool_properties(self):
        registry = create_dwsim_tool_registry()
        for tool in registry.tools:
            assert tool.server_name == "dwsim"
            assert tool.module.startswith("dwsim_tools.tools.")
            assert len(tool.return_spec) >= 2  # at minimum success + error

    @pytest.mark.unit
    def test_flowsheet_parameters(self):
        """Tools that accept a flowsheet param should declare it as object type."""
        registry = create_dwsim_tool_registry()
        tools_with_flowsheet_param = [
            t for t in registry.tools if any(p.name == "flowsheet" for p in t.required_parameters)
        ]
        # All tools except create_flowsheet and load_flowsheet take a flowsheet param
        assert len(tools_with_flowsheet_param) >= 17

        for tool in tools_with_flowsheet_param:
            fs_param = next(p for p in tool.required_parameters if p.name == "flowsheet")
            assert fs_param.type is object

    @pytest.mark.unit
    def test_flowsheet_returned(self):
        """create_flowsheet and load_flowsheet should return a flowsheet."""
        registry = create_dwsim_tool_registry()
        for name in ["create_flowsheet", "load_flowsheet"]:
            tool = next(t for t in registry.tools if t.name == name)
            fs_ret = next(r for r in tool.return_spec if r.name == "flowsheet")
            assert fs_ret.type is object
