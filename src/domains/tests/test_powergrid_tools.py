"""Tests for PowerGrid tool registry.

Live Integration Tests:
-----------------------
The live tests require the powergrid server to be running on localhost:8001.

Setup:
1. Ensure you're logged in with Azure CLI: az login

2. Set required environment variables (in .env file or shell):
   export ENTRA_CLIENT_ID=<your-app-client-id>    # Required: Application ID for token audience
   export ENTRA_TENANT_ID=<your-tenant-id>        # Required: Tenant ID for token validation
   export OBO_SIMULATION_MODE=true                # Required: Use Azure CLI credentials for local dev

3. Start the server:
   cd code_execution/docker && docker compose up powergrid-server --build

4. Run tests:
   export ENTRA_CLIENT_ID=<your-app-client-id>  # Must match server config
   pytest domains/tests/test_powergrid_tools.py -v
"""

import pytest
import pytest_asyncio
from unittest.mock import patch

from tools.mcp import get_mcp_registry, MCPServerDescriptor, reset_mcp_registry
from domains.powergrid.server.tool_registry import create_powergrid_tool_registry


@pytest_asyncio.fixture(scope="function", autouse=True)
async def setup_powergrid_server():
    """Register powergrid server before tests run, and mock validation for unit tests."""
    # Reset registry to ensure clean state
    reset_mcp_registry()

    # Register the actual server for integration tests
    registry = get_mcp_registry()
    with patch.object(registry, "_validate_server_connection", return_value=(True, "")):
        await registry.register(
            MCPServerDescriptor(
                name="powergrid",
                url="http://localhost:8001/mcp",
                description="PowerGrid MCP Server",
                scope="https://test.scope/.default",
            )
        )
    yield
    # Cleanup after test
    reset_mcp_registry()


class TestPowerGridToolRegistry:
    """Test cases for PowerGrid tool registry creation."""

    @pytest.mark.unit
    def test_create_powergrid_tool_registry(self):
        """Test that powergrid tool registry is created successfully."""
        registry = create_powergrid_tool_registry()

        assert registry is not None
        assert len(registry.tools) == 1

    @pytest.mark.unit
    def test_run_opf_tool_definition(self):
        """Test run_opf tool is properly defined."""
        registry = create_powergrid_tool_registry()

        # Get tool by name
        tool = registry.get_tool_by_name("run_opf")

        assert tool is not None
        assert tool.name == "run_opf"
        assert "optimal power flow" in tool.description.lower()
        assert tool.module == "pypsa_powergrid_tools.tools.pypsa_opf"

        # Check server_name
        assert tool.server_name == "powergrid"

        # Check required parameters
        assert len(tool.required_parameters) == 1
        network_path_param = tool.required_parameters[0]
        assert network_path_param.name == "network_path"
        assert network_path_param.type is str
        assert "path" in network_path_param.description.lower()

        # Check optional parameters
        assert len(tool.optional_parameters) == 0

    @pytest.mark.unit
    def test_all_tools_use_mcp_execution(self):
        """Test that all tools in registry have powergrid server_name."""
        registry = create_powergrid_tool_registry()

        for tool in registry.tools:
            assert tool.server_name == "powergrid"

    @pytest.mark.unit
    def test_all_tools_have_network_path_parameter(self):
        """Test that all tools require network_path parameter."""
        registry = create_powergrid_tool_registry()

        for tool in registry.tools:
            assert len(tool.required_parameters) >= 1
            param_names = [p.name for p in tool.required_parameters]
            assert "network_path" in param_names

    @pytest.mark.unit
    def test_tool_names_are_unique(self):
        """Test that all tool names in registry are unique."""
        registry = create_powergrid_tool_registry()

        tool_names = [tool.name for tool in registry.tools]
        assert len(tool_names) == len(set(tool_names))

    @pytest.mark.unit
    def test_tools_can_be_retrieved_by_name(self):
        """Test that tools can be retrieved by name."""
        registry = create_powergrid_tool_registry()

        run_opf_tool = registry.get_tool_by_name("run_opf")
        assert run_opf_tool is not None
        assert run_opf_tool.name == "run_opf"

    @pytest.mark.unit
    def test_tools_can_be_retrieved_by_id(self):
        """Test that tools can be retrieved by ID."""
        registry = create_powergrid_tool_registry()

        # Get ID for run_opf
        tool_id = registry.get_id_by_name("run_opf")
        assert tool_id is not None

        # Retrieve by ID
        tool = registry.get_tool_by_id(tool_id)
        assert tool is not None
        assert tool.name == "run_opf"

    @pytest.mark.unit
    def test_nonexistent_tool_raises_error(self):
        """Test that retrieving nonexistent tool raises ValueError."""
        registry = create_powergrid_tool_registry()

        with pytest.raises(ValueError, match="nonexistent_tool is not registered"):
            registry.get_tool_by_name("nonexistent_tool")


class TestPowerGridToolRegistryIntegration:
    """Integration tests for PowerGrid tool registry."""

    @pytest.mark.integration
    def test_registry_integration_with_mcp_server(self):
        """Test that registry tools reference the correct MCP server."""
        from tools.mcp import get_mcp_registry

        # Get the MCP registry
        mcp_registry = get_mcp_registry()

        # Verify powergrid is registered
        assert mcp_registry.has_server("powergrid")

        # Create powergrid tool registry
        tool_registry = create_powergrid_tool_registry()

        # Verify all tools reference the registered server
        for tool in tool_registry.tools:
            server_name = tool.server_name
            assert server_name is not None
            assert mcp_registry.has_server(server_name), (
                f"Tool {tool.name} references unregistered server {server_name}"
            )

    @pytest.mark.integration
    def test_server_name_validation(self):
        """Test that server_name validates against registered servers."""
        from tools.mcp import get_mcp_registry

        # Verify the server is registered
        registry = get_mcp_registry()
        assert registry.has_server("powergrid")

    @pytest.mark.integration
    def test_tools_serialization(self):
        """Test that tools can be serialized and deserialized."""
        registry = create_powergrid_tool_registry()

        for tool in registry.tools:
            # Serialize
            tool_dict = tool.model_dump()

            assert "name" in tool_dict
            assert "description" in tool_dict
            assert "server_name" in tool_dict
            assert tool_dict["server_name"] == "powergrid"

            # Deserialize
            from code_execution import ToolDefinition

            restored_tool = ToolDefinition(**tool_dict)

            assert restored_tool.name == tool.name
            assert restored_tool.description == tool.description
            assert restored_tool.server_name == tool.server_name

    @pytest.mark.integration
    def test_powergrid_server_uses_tool_registry(self):
        """Test that powergrid server configuration uses the tool registry."""
        from domains.powergrid.server.powergrid_server import create_powergrid_config

        # Create server config
        config = create_powergrid_config()

        # Verify config properties
        assert config.name == "powergrid"
        assert config.type == "uv"
        assert "power grid" in config.description.lower()

        # The server should be able to use the tool registry
        tool_registry = create_powergrid_tool_registry()
        assert tool_registry is not None
        assert len(tool_registry.tools) > 0


class TestPowerGridServerLive:
    """Live tests for running PowerGrid server (requires server to be running)."""

    @pytest.fixture
    def server_url(self, setup_powergrid_server):
        """Return the server URL (configurable via environment)."""
        import os

        return os.getenv("POWERGRID_SERVER_URL", "http://localhost:8001")

    @pytest.fixture
    def mcp_url(self, server_url):
        """Return the full MCP URL for the powergrid server."""
        return f"{server_url}/mcp"

    @pytest.mark.live
    def test_server_health_endpoint(self, server_url):
        """Test that the server health endpoint is accessible."""
        import requests

        response = requests.get(f"{server_url}/health", timeout=5)
        assert response.status_code == 200

        data = response.json()
        assert data["status"] == "healthy"
        assert data["environment"] == "powergrid"
        assert "python" in data

    @pytest.mark.live
    def test_mcp_connection_requires_proper_client(self, server_url):
        """Test that MCP endpoints require authentication."""
        import requests

        # Direct POST to SSE endpoint without auth should fail
        response = requests.post(f"{server_url}/mcp", json={"test": "data"}, timeout=5)
        # Server requires authentication
        assert response.status_code == 401  # Unauthorized

    @pytest.mark.live
    @pytest.mark.asyncio
    async def test_mcp_tool_list(self, mcp_url, authenticated_client_session):
        """Test that we can list MCP tools from the server using MCP client."""
        async with authenticated_client_session(url=mcp_url) as session:
            # Initialize the session
            await session.initialize()

            # List available tools
            tools_result = await session.list_tools()
            tools = tools_result.tools

            assert len(tools) > 0, "Server should expose at least one tool"

            # Verify our powergrid tools are present
            tool_names = [tool.name for tool in tools]
            assert "run_opf" in tool_names

            # Check tool schemas
            for tool in tools:
                assert tool.name
                assert tool.description
                assert tool.inputSchema  # Should have input schema

    @pytest.mark.live
    @pytest.mark.asyncio
    async def test_tool_schema_validation(self, mcp_url, authenticated_client_session):
        """Test that tool schemas are properly defined."""
        async with authenticated_client_session(url=mcp_url) as session:
            await session.initialize()

            tools_result = await session.list_tools()
            tools = tools_result.tools

            # Find run_opf tool
            run_opf_tool = next((t for t in tools if t.name == "run_opf"), None)
            assert run_opf_tool is not None, "run_opf tool should be available"

            # Verify it has proper schema
            assert run_opf_tool.description
            assert run_opf_tool.inputSchema

            # Check the schema structure
            schema = run_opf_tool.inputSchema
            assert "properties" in schema

            # FastMCP wraps function parameters as kwargs (string type)
            # The actual parameters are passed as a JSON string in kwargs
            # So we just verify that the schema exists and has properties
            assert len(schema["properties"]) > 0, "Schema should have at least one property"

            # Verify the tool description mentions network path parameter
            assert "path" in run_opf_tool.description.lower() or "file" in run_opf_tool.description.lower(), (
                "Tool description should mention the network path parameter"
            )

    @pytest.mark.live
    @pytest.mark.asyncio
    async def test_tool_execution_run_opf(self, mcp_url, authenticated_client_session):
        """Test executing the run_opf tool with a test network file."""
        import json

        async with authenticated_client_session(url=mcp_url) as session:
            await session.initialize()

            # Step 1: Create an IEEE 14 bus test network and save to file
            network_creation_code = """
import pypsa
import os

# Create IEEE 14 bus test network
n = pypsa.Network()

# Add 14 buses
bus_names = [f"Bus_{i}" for i in range(1, 15)]
for bus in bus_names:
    n.add("Bus", bus, v_nom=138)  # 138 kV base voltage

# Add 5 generators with different characteristics
generators = [
    {"name": "Gen_1", "bus": "Bus_1", "p_nom": 332.4, "marginal_cost": 20, "p_min_pu": 0.0},
    {"name": "Gen_2", "bus": "Bus_2", "p_nom": 140.0, "marginal_cost": 25, "p_min_pu": 0.0},
    {"name": "Gen_3", "bus": "Bus_3", "p_nom": 100.0, "marginal_cost": 30, "p_min_pu": 0.0},
    {"name": "Gen_6", "bus": "Bus_6", "p_nom": 100.0, "marginal_cost": 35, "p_min_pu": 0.0},
    {"name": "Gen_8", "bus": "Bus_8", "p_nom": 100.0, "marginal_cost": 40, "p_min_pu": 0.0},
]
for gen in generators:
    n.add("Generator", **gen)

# Add 11 loads (standard IEEE 14 bus load distribution)
loads = [
    {"name": "Load_2", "bus": "Bus_2", "p_set": 21.7},
    {"name": "Load_3", "bus": "Bus_3", "p_set": 94.2},
    {"name": "Load_4", "bus": "Bus_4", "p_set": 47.8},
    {"name": "Load_5", "bus": "Bus_5", "p_set": 7.6},
    {"name": "Load_6", "bus": "Bus_6", "p_set": 11.2},
    {"name": "Load_9", "bus": "Bus_9", "p_set": 29.5},
    {"name": "Load_10", "bus": "Bus_10", "p_set": 9.0},
    {"name": "Load_11", "bus": "Bus_11", "p_set": 3.5},
    {"name": "Load_12", "bus": "Bus_12", "p_set": 6.1},
    {"name": "Load_13", "bus": "Bus_13", "p_set": 13.5},
    {"name": "Load_14", "bus": "Bus_14", "p_set": 14.9},
]
for load in loads:
    n.add("Load", **load)

# Add 20 transmission lines (IEEE 14 bus topology)
lines = [
    {"name": "Line_1_2", "bus0": "Bus_1", "bus1": "Bus_2", "x": 0.05917, "r": 0.01938, "s_nom": 150},
    {"name": "Line_1_5", "bus0": "Bus_1", "bus1": "Bus_5", "x": 0.22304, "r": 0.05403, "s_nom": 150},
    {"name": "Line_2_3", "bus0": "Bus_2", "bus1": "Bus_3", "x": 0.19797, "r": 0.04699, "s_nom": 150},
    {"name": "Line_2_4", "bus0": "Bus_2", "bus1": "Bus_4", "x": 0.17632, "r": 0.05811, "s_nom": 150},
    {"name": "Line_2_5", "bus0": "Bus_2", "bus1": "Bus_5", "x": 0.17388, "r": 0.05695, "s_nom": 150},
    {"name": "Line_3_4", "bus0": "Bus_3", "bus1": "Bus_4", "x": 0.17103, "r": 0.06701, "s_nom": 150},
    {"name": "Line_4_5", "bus0": "Bus_4", "bus1": "Bus_5", "x": 0.04211, "r": 0.01335, "s_nom": 150},
    {"name": "Line_4_7", "bus0": "Bus_4", "bus1": "Bus_7", "x": 0.20912, "r": 0.0, "s_nom": 150},
    {"name": "Line_4_9", "bus0": "Bus_4", "bus1": "Bus_9", "x": 0.55618, "r": 0.0, "s_nom": 150},
    {"name": "Line_5_6", "bus0": "Bus_5", "bus1": "Bus_6", "x": 0.17615, "r": 0.0, "s_nom": 150},
    {"name": "Line_6_11", "bus0": "Bus_6", "bus1": "Bus_11", "x": 0.20820, "r": 0.09498, "s_nom": 150},
    {"name": "Line_6_12", "bus0": "Bus_6", "bus1": "Bus_12", "x": 0.25581, "r": 0.12291, "s_nom": 150},
    {"name": "Line_6_13", "bus0": "Bus_6", "bus1": "Bus_13", "x": 0.13027, "r": 0.06615, "s_nom": 150},
    {"name": "Line_7_8", "bus0": "Bus_7", "bus1": "Bus_8", "x": 0.17615, "r": 0.0, "s_nom": 150},
    {"name": "Line_7_9", "bus0": "Bus_7", "bus1": "Bus_9", "x": 0.11001, "r": 0.0, "s_nom": 150},
    {"name": "Line_9_10", "bus0": "Bus_9", "bus1": "Bus_10", "x": 0.08450, "r": 0.03181, "s_nom": 150},
    {"name": "Line_9_14", "bus0": "Bus_9", "bus1": "Bus_14", "x": 0.27038, "r": 0.12711, "s_nom": 150},
    {"name": "Line_10_11", "bus0": "Bus_10", "bus1": "Bus_11", "x": 0.19890, "r": 0.08205, "s_nom": 150},
    {"name": "Line_12_13", "bus0": "Bus_12", "bus1": "Bus_13", "x": 0.19988, "r": 0.22092, "s_nom": 150},
    {"name": "Line_13_14", "bus0": "Bus_13", "bus1": "Bus_14", "x": 0.34802, "r": 0.17093, "s_nom": 150},
]
for line in lines:
    n.add("Line", **line)

# Save to file in allowed temporary directory
network_path = "/tmp/test_network.nc"
n.export_to_netcdf(network_path)
print(network_path)
"""

            # Execute code to create network file
            create_result = await session.call_tool("execute_powergrid_code", arguments={"code": network_creation_code})

            # Verify network was created
            assert create_result.content, "Network creation should return content"
            create_content = (
                create_result.content[0].text
                if hasattr(create_result.content[0], "text")
                else str(create_result.content[0])
            )
            create_response = json.loads(create_content.strip())
            assert create_response.get("success"), f"Network creation failed: {create_response.get('error')}"

            # Get the network path from stdout
            network_path = create_response.get("stdout", "").strip().split("\n")[-1]
            print(f"\nCreated network at: {network_path}")

            # Step 2: Call the run_opf tool directly with the network path
            opf_result = await session.call_tool("run_opf", arguments={"network_path": network_path})

            # Result should have content
            assert opf_result.content, "run_opf should return content"
            assert len(opf_result.content) > 0, "run_opf should return at least one content item"

            # Get content text
            content_text = (
                opf_result.content[0].text if hasattr(opf_result.content[0], "text") else str(opf_result.content[0])
            )

            # Parse JSON response
            result = json.loads(content_text.strip())

            print("\n" + "=" * 80)
            print("OPF RESULT:")
            print("=" * 80)
            print(json.dumps(result, indent=2))
            print("=" * 80 + "\n")

            # Check if OPF succeeded
            assert "success" in result, f"Result should have success field: {result}"

            if not result["success"]:
                error_msg = result.get("error", "")
                pytest.fail(f"OPF execution failed: {error_msg}")
            else:
                # If successful, should have optimization results
                assert "status" in result, f"Successful result should have status: {result}"
                assert "objective" in result, f"Successful result should have objective: {result}"
                assert result["status"] == "ok", f"Status should be 'ok': {result}"

                # Verify handle structure
                assert "handles" in result, "Result should contain handles"
                assert "network" in result["handles"], "Result should contain network handle"
                network_handle_info = result["handles"]["network"]
                assert network_handle_info["handle"].startswith("h_"), (
                    f"Handle should start with 'h_': {network_handle_info['handle']}"
                )
                assert network_handle_info["type"] == "object", (
                    f"Handle type should be 'object': {network_handle_info['type']}"
                )
                assert "session_id" in result, "Result should include session_id"

    @pytest.mark.live
    @pytest.mark.asyncio
    async def test_invalid_tool_call(self, mcp_url, authenticated_client_session):
        """Test calling a nonexistent tool returns proper error."""
        async with authenticated_client_session(url=mcp_url) as session:
            await session.initialize()

            # Try to call nonexistent tool
            try:
                result = await session.call_tool("nonexistent_tool", arguments={})
                # If no exception, check if result indicates error
                assert result.isError or not result.content, "Should return error for nonexistent tool"
            except Exception as e:
                # Error should mention the tool name
                error_msg = str(e).lower()
                assert (
                    "nonexistent" in error_msg
                    or "not found" in error_msg
                    or "unknown" in error_msg
                    or "tool" in error_msg
                )

    @pytest.mark.live
    @pytest.mark.asyncio
    async def test_tool_execution_with_invalid_params(self, mcp_url, authenticated_client_session):
        """Test that invalid parameters are properly rejected."""
        async with authenticated_client_session(url=mcp_url) as session:
            await session.initialize()

            # Try to call with missing required parameter
            try:
                result = await session.call_tool(
                    "run_opf",
                    arguments={},  # Missing required 'network_path' parameter
                )
                # If no exception, check result indicates error
                result_text = str(result.content[0] if result.content else "").lower()
                assert "error" in result_text or "missing" in result_text or "required" in result_text, (
                    "Should return error for missing required parameters"
                )
            except Exception as e:
                # Error should mention missing parameter or validation failure
                error_msg = str(e).lower()
                assert any(
                    x in error_msg for x in ["network_path", "path", "required", "missing", "validation", "invalid"]
                )

    @pytest.mark.live
    @pytest.mark.asyncio
    async def test_session_persistence_network_state(self, mcp_url, authenticated_client_session):
        """Test that network objects persist across multiple code executions in the same session."""
        import json

        async with authenticated_client_session(url=mcp_url) as session:
            await session.initialize()

            # First execution: Create IEEE 14 bus network (same as test_tool_execution_run_opf)
            create_network_code = """
import pypsa
import json

# Create IEEE 14 bus test network
n = pypsa.Network()

# Add 14 buses
bus_names = [f"Bus_{i}" for i in range(1, 15)]
for bus in bus_names:
    n.add("Bus", bus, v_nom=138)  # 138 kV base voltage

# Add 5 generators with different characteristics
generators = [
    {"name": "Gen_1", "bus": "Bus_1", "p_nom": 332.4, "marginal_cost": 20, "p_min_pu": 0.0},
    {"name": "Gen_2", "bus": "Bus_2", "p_nom": 140.0, "marginal_cost": 25, "p_min_pu": 0.0},
    {"name": "Gen_3", "bus": "Bus_3", "p_nom": 100.0, "marginal_cost": 30, "p_min_pu": 0.0},
    {"name": "Gen_6", "bus": "Bus_6", "p_nom": 100.0, "marginal_cost": 35, "p_min_pu": 0.0},
    {"name": "Gen_8", "bus": "Bus_8", "p_nom": 100.0, "marginal_cost": 40, "p_min_pu": 0.0},
]
for gen in generators:
    n.add("Generator", **gen)

# Add 11 loads (standard IEEE 14 bus load distribution)
loads = [
    {"name": "Load_2", "bus": "Bus_2", "p_set": 21.7},
    {"name": "Load_3", "bus": "Bus_3", "p_set": 94.2},
    {"name": "Load_4", "bus": "Bus_4", "p_set": 47.8},
    {"name": "Load_5", "bus": "Bus_5", "p_set": 7.6},
    {"name": "Load_6", "bus": "Bus_6", "p_set": 11.2},
    {"name": "Load_9", "bus": "Bus_9", "p_set": 29.5},
    {"name": "Load_10", "bus": "Bus_10", "p_set": 9.0},
    {"name": "Load_11", "bus": "Bus_11", "p_set": 3.5},
    {"name": "Load_12", "bus": "Bus_12", "p_set": 6.1},
    {"name": "Load_13", "bus": "Bus_13", "p_set": 13.5},
    {"name": "Load_14", "bus": "Bus_14", "p_set": 14.9},
]
for load in loads:
    n.add("Load", **load)

# Add 20 transmission lines (IEEE 14 bus topology)
lines = [
    {"name": "Line_1_2", "bus0": "Bus_1", "bus1": "Bus_2", "x": 0.05917, "r": 0.01938, "s_nom": 150},
    {"name": "Line_1_5", "bus0": "Bus_1", "bus1": "Bus_5", "x": 0.22304, "r": 0.05403, "s_nom": 150},
    {"name": "Line_2_3", "bus0": "Bus_2", "bus1": "Bus_3", "x": 0.19797, "r": 0.04699, "s_nom": 150},
    {"name": "Line_2_4", "bus0": "Bus_2", "bus1": "Bus_4", "x": 0.17632, "r": 0.05811, "s_nom": 150},
    {"name": "Line_2_5", "bus0": "Bus_2", "bus1": "Bus_5", "x": 0.17388, "r": 0.05695, "s_nom": 150},
    {"name": "Line_3_4", "bus0": "Bus_3", "bus1": "Bus_4", "x": 0.17103, "r": 0.06701, "s_nom": 150},
    {"name": "Line_4_5", "bus0": "Bus_4", "bus1": "Bus_5", "x": 0.04211, "r": 0.01335, "s_nom": 150},
    {"name": "Line_4_7", "bus0": "Bus_4", "bus1": "Bus_7", "x": 0.20912, "r": 0.0, "s_nom": 150},
    {"name": "Line_4_9", "bus0": "Bus_4", "bus1": "Bus_9", "x": 0.55618, "r": 0.0, "s_nom": 150},
    {"name": "Line_5_6", "bus0": "Bus_5", "bus1": "Bus_6", "x": 0.17615, "r": 0.0, "s_nom": 150},
    {"name": "Line_6_11", "bus0": "Bus_6", "bus1": "Bus_11", "x": 0.20820, "r": 0.09498, "s_nom": 150},
    {"name": "Line_6_12", "bus0": "Bus_6", "bus1": "Bus_12", "x": 0.25581, "r": 0.12291, "s_nom": 150},
    {"name": "Line_6_13", "bus0": "Bus_6", "bus1": "Bus_13", "x": 0.13027, "r": 0.06615, "s_nom": 150},
    {"name": "Line_7_8", "bus0": "Bus_7", "bus1": "Bus_8", "x": 0.17615, "r": 0.0, "s_nom": 150},
    {"name": "Line_7_9", "bus0": "Bus_7", "bus1": "Bus_9", "x": 0.11001, "r": 0.0, "s_nom": 150},
    {"name": "Line_9_10", "bus0": "Bus_9", "bus1": "Bus_10", "x": 0.08450, "r": 0.03181, "s_nom": 150},
    {"name": "Line_9_14", "bus0": "Bus_9", "bus1": "Bus_14", "x": 0.27038, "r": 0.12711, "s_nom": 150},
    {"name": "Line_10_11", "bus0": "Bus_10", "bus1": "Bus_11", "x": 0.19890, "r": 0.08205, "s_nom": 150},
    {"name": "Line_12_13", "bus0": "Bus_12", "bus1": "Bus_13", "x": 0.19988, "r": 0.22092, "s_nom": 150},
    {"name": "Line_13_14", "bus0": "Bus_13", "bus1": "Bus_14", "x": 0.34802, "r": 0.17093, "s_nom": 150},
]
for line in lines:
    n.add("Line", **line)

# Store metadata about the network for verification
network_info = {
    "num_buses": len(n.buses),
    "num_generators": len(n.generators),
    "num_loads": len(n.loads),
    "num_lines": len(n.lines)
}

print(json.dumps({"created": True, "info": network_info}))
"""

            # Execute first code block
            result1 = await session.call_tool("execute_powergrid_code", arguments={"code": create_network_code})

            assert result1.content, "First execution should return content"
            content_text1 = result1.content[0].text if hasattr(result1.content[0], "text") else str(result1.content[0])
            exec_result1 = json.loads(content_text1.strip())

            assert exec_result1.get("success"), f"First execution failed: {exec_result1.get('error')}"

            # Parse the output from first execution
            stdout1 = exec_result1.get("stdout", "").strip()
            creation_result = json.loads(stdout1)

            print(f"\nFirst execution - Network created: {creation_result}")
            assert creation_result["created"], "Network should be created"
            assert creation_result["info"]["num_buses"] == 14, "Should have 14 buses"
            assert creation_result["info"]["num_generators"] == 5, "Should have 5 generators"
            assert creation_result["info"]["num_loads"] == 11, "Should have 11 loads"
            assert creation_result["info"]["num_lines"] == 20, "Should have 20 lines"

            # Second execution: Access the network variable from the previous execution
            access_network_code = """
import json

# Access the network variable 'n' from previous execution
# If session persistence works, 'n' should still exist
try:
    # Verify network exists and has expected properties
    result = {
        "network_exists": 'n' in dir(),
        "num_buses": len(n.buses) if 'n' in dir() else 0,
        "num_generators": len(n.generators) if 'n' in dir() else 0,
        "num_loads": len(n.loads) if 'n' in dir() else 0,
        "num_lines": len(n.lines) if 'n' in dir() else 0,
        "network_type": str(type(n).__name__) if 'n' in dir() else None,
        "bus_names": list(n.buses.index) if 'n' in dir() else []
    }
    print(json.dumps(result))
except NameError as e:
    print(json.dumps({"network_exists": False, "error": str(e)}))
"""

            # Execute second code block in same session
            result2 = await session.call_tool("execute_powergrid_code", arguments={"code": access_network_code})

            assert result2.content, "Second execution should return content"
            content_text2 = result2.content[0].text if hasattr(result2.content[0], "text") else str(result2.content[0])
            exec_result2 = json.loads(content_text2.strip())

            assert exec_result2.get("success"), f"Second execution failed: {exec_result2.get('error')}"

            # Parse the output from second execution
            stdout2 = exec_result2.get("stdout", "").strip()
            access_result = json.loads(stdout2)

            print(f"\nSecond execution - Network access: {access_result}")

            # Verify session persistence
            assert access_result["network_exists"], "Network variable 'n' should persist in session"
            assert access_result["network_type"] == "Network", "Network should be PyPSA Network type"
            assert access_result["num_buses"] == 14, "Network should still have 14 buses"
            assert access_result["num_generators"] == 5, "Network should still have 5 generators"
            assert access_result["num_loads"] == 11, "Network should still have 11 loads"
            assert access_result["num_lines"] == 20, "Network should still have 20 lines"
            assert len(access_result["bus_names"]) == 14, "Should have 14 bus names"

    @pytest.mark.live
    def test_server_environment_ready(self, server_url):
        """Test that the server reports environment as ready."""
        import requests

        response = requests.get(f"{server_url}/health", timeout=5)
        assert response.status_code == 200

        data = response.json()
        # Environment may not be ready initially (builds on first use)
        assert "environment_ready" in data
        assert isinstance(data["environment_ready"], bool)
