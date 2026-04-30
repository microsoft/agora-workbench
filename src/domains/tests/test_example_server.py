"""
Integration tests for example server with programmatic tool use.

These tests verify that domain tools are available as Python functions
inside the code execution environment, and that the MCP interface
only exposes infrastructure tools (execute_code, session management,
domain tool catalog).

These tests require the example server to be running on localhost:8000.

Setup:
1. Ensure you're logged in with Azure CLI: az login

2. Set required environment variables (in .env file or shell):
   export ENTRA_CLIENT_ID=<your-app-client-id>    # Required: Application ID for token audience
   export ENTRA_TENANT_ID=<your-tenant-id>        # Required: Tenant ID for token validation
   export OBO_SIMULATION_MODE=true                # Required: Use Azure CLI credentials for local dev

3. Start the server:
   cd code_execution/docker && docker compose up example-server --build

   OR run directly:
   export OBO_SIMULATION_MODE=true ENTRA_CLIENT_ID=<id> ENTRA_TENANT_ID=<id>
   python -m domains.example.server.example_server

4. Run tests:
   export ENTRA_CLIENT_ID=<your-app-client-id>  # Must match server config
   pytest domains/tests/test_example_server.py -v
"""

import json
import logging
import pytest

# Suppress httpx logging during tests
logging.getLogger("httpx").setLevel(logging.WARNING)


def parse_tool_result(result):
    """Parse MCP tool call result and extract the response data."""
    assert len(result.content) > 0
    response_text = result.content[0].text
    return json.loads(response_text)


@pytest.mark.live
@pytest.mark.asyncio
class TestExampleServerTools:
    """Test MCP tool surface — only infrastructure tools should be exposed."""

    async def test_server_connection(self, authenticated_client_session):
        """Test that we can connect and list tools; domain tools are NOT exposed via MCP."""

        async with authenticated_client_session() as session:
            await session.initialize()
            response = await session.list_tools()

        tool_names = [tool.name for tool in response.tools]

        # Infrastructure tools exposed via MCP
        assert "execute_example_code" in tool_names
        assert "list_example_domain_tools" in tool_names
        assert "example_list_sessions" in tool_names
        assert "example_get_session_info" in tool_names
        assert "example_close_session" in tool_names

        # Domain tools should NOT appear as MCP tools — they are called
        # programmatically inside execute_example_code
        assert "create_counter" not in tool_names
        assert "increment_counter" not in tool_names
        assert "get_counter_value" not in tool_names
        assert "calculate_fibonacci" not in tool_names

    async def test_domain_tool_catalog(self, authenticated_client_session):
        """Test that the domain tool catalog meta-tool returns all domain tools."""
        async with authenticated_client_session() as session:
            await session.initialize()
            result = await session.call_tool("list_example_domain_tools")

        catalog = json.loads(result.content[0].text)
        tool_names = [t["name"] for t in catalog]

        assert "create_counter" in tool_names
        assert "increment_counter" in tool_names
        assert "get_counter_value" in tool_names
        assert "calculate_fibonacci" in tool_names

    async def test_stateless_tool_via_code(self, authenticated_client_session):
        """Test executing a stateless domain tool via execute_example_code."""
        code = "result = calculate_fibonacci(n=10)\nprint(result)"

        async with authenticated_client_session() as session:
            await session.initialize()
            result = await session.call_tool("execute_example_code", arguments={"code": code, "timeout": 10})

        response_data = parse_tool_result(result)
        assert response_data["success"] is True
        assert "sequence" in response_data["stdout"] or "[0, 1, 1" in response_data["stdout"]


@pytest.mark.live
@pytest.mark.asyncio
class TestProgrammaticToolUse:
    """Test calling domain tools programmatically inside execute_example_code."""

    async def test_create_and_use_counter(self, authenticated_client_session):
        """Test creating a counter and using it within a single code execution."""
        code = """
result = create_counter(initial_value=42)
counter = result['counter']
val = get_counter_value(counter=counter)
print(f"value={val['result']}")
"""
        async with authenticated_client_session() as session:
            await session.initialize()
            result = await session.call_tool("execute_example_code", arguments={"code": code, "timeout": 10})

        response_data = parse_tool_result(result)
        assert response_data["success"] is True
        assert "value=42" in response_data["stdout"]

    async def test_increment_counter(self, authenticated_client_session):
        """Test counter increment via programmatic tool call."""
        code = """
result = create_counter(initial_value=10)
counter = result['counter']
inc = increment_counter(counter=counter, amount=5)
print(f"result={inc['result']}")
"""
        async with authenticated_client_session() as session:
            await session.initialize()
            result = await session.call_tool("execute_example_code", arguments={"code": code, "timeout": 10})

        response_data = parse_tool_result(result)
        assert response_data["success"] is True
        assert "result=15" in response_data["stdout"]

    async def test_multiple_counters_independent(self, authenticated_client_session):
        """Test that multiple counters are independent objects."""
        code = """
c1 = create_counter(initial_value=100)['counter']
c2 = create_counter(initial_value=200)['counter']

v1 = get_counter_value(counter=c1)['result']
v2 = get_counter_value(counter=c2)['result']
print(f"c1={v1} c2={v2}")

inc1 = increment_counter(counter=c1, amount=10)['result']
inc2 = increment_counter(counter=c2, amount=20)['result']
print(f"inc1={inc1} inc2={inc2}")
"""
        async with authenticated_client_session() as session:
            await session.initialize()
            result = await session.call_tool("execute_example_code", arguments={"code": code, "timeout": 10})

        response_data = parse_tool_result(result)
        assert response_data["success"] is True
        assert "c1=100 c2=200" in response_data["stdout"]
        assert "inc1=110 inc2=220" in response_data["stdout"]

    async def test_create_pair_and_combine(self, authenticated_client_session):
        """Test creating a pair and combining values."""
        code = """
pair = create_pair(first=10, second=20)
h1 = pair['first_handle']
h2 = pair['second_handle']

add_result = combine_handles(first=h1, second=h2, operation='add')
print(f"sum={add_result['result']}")

mult_result = combine_handles(first=h1, second=h2, operation='multiply')
print(f"product={mult_result['result']}")
"""
        async with authenticated_client_session() as session:
            await session.initialize()
            result = await session.call_tool("execute_example_code", arguments={"code": code, "timeout": 10})

        response_data = parse_tool_result(result)
        assert response_data["success"] is True
        assert "sum=30" in response_data["stdout"]
        assert "product=200" in response_data["stdout"]

    async def test_transform_and_create(self, authenticated_client_session):
        """Test tool that transforms a value and creates a new object."""
        code = """
counter = create_counter(initial_value=10)['counter']
transformed = transform_and_create(input_value=counter, multiplier=3)
new_val = get_counter_value(counter=transformed['transformed'])
print(f"original=10 transformed={new_val['result']}")
"""
        async with authenticated_client_session() as session:
            await session.initialize()
            result = await session.call_tool("execute_example_code", arguments={"code": code, "timeout": 10})

        response_data = parse_tool_result(result)
        assert response_data["success"] is True
        assert "original=10 transformed=30" in response_data["stdout"]

    async def test_chained_workflow(self, authenticated_client_session):
        """Test complex chained workflow using multiple tools."""
        code = """
pair = create_pair(first=5, second=8)
h1 = pair['first_handle']
h2 = pair['second_handle']

# Transform first (5 * 2 = 10)
t1 = transform_and_create(input_value=h1, multiplier=2)
h1_t = t1['transformed']

# Combine transformed with second (10 + 8 = 18)
combined = combine_handles(first=h1_t, second=h2, operation='add')
print(f"combined={combined['result']}")

# Originals unchanged
v1 = get_counter_value(counter=h1)['result']
v2 = get_counter_value(counter=h2)['result']
print(f"originals={v1},{v2}")
"""
        async with authenticated_client_session() as session:
            await session.initialize()
            result = await session.call_tool("execute_example_code", arguments={"code": code, "timeout": 10})

        response_data = parse_tool_result(result)
        assert response_data["success"] is True
        assert "combined=18" in response_data["stdout"]
        assert "originals=5,8" in response_data["stdout"]

    async def test_tool_call_tracing(self, authenticated_client_session):
        """Test that tool calls made in code are traced in the response."""
        code = """
result = calculate_fibonacci(n=5)
print(f"count={result['count']}")
"""
        async with authenticated_client_session() as session:
            await session.initialize()
            result = await session.call_tool("execute_example_code", arguments={"code": code, "timeout": 10})

        response_data = parse_tool_result(result)
        assert response_data["success"] is True
        assert "count=5" in response_data["stdout"]

        # Verify tool call tracing
        assert "tool_calls" in response_data
        tool_calls = response_data["tool_calls"]
        assert len(tool_calls) >= 1
        assert tool_calls[0]["tool_name"] == "calculate_fibonacci"
        assert tool_calls[0]["success"] is True


@pytest.mark.live
@pytest.mark.asyncio
class TestExecuteCodeWithSessions:
    """Test the execute_code_tool with automatic session management."""

    async def test_execute_code_basic(self, authenticated_client_session):
        """Test basic code execution."""
        async with authenticated_client_session() as session:
            await session.initialize()
            result = await session.call_tool(
                "execute_example_code", arguments={"code": "x = 42\nprint(x)", "timeout": 10}
            )

        response_data = parse_tool_result(result)

        assert response_data["success"] is True
        assert "42" in response_data["stdout"]

    async def test_execute_code_persistence(self, authenticated_client_session):
        """Test that variables persist across execute_code calls within same session."""
        async with authenticated_client_session() as session:
            await session.initialize()

            # First execution: define variable
            result1 = await session.call_tool(
                "execute_example_code", arguments={"code": "persistent_var = 'hello world'", "timeout": 10}
            )

            data1 = json.loads(result1.content[0].text)
            assert data1["success"] is True

            # Second execution: use the variable (should work if in same session)
            # Note: Session persistence depends on @auto_session_tool decorator
            result2 = await session.call_tool(
                "execute_example_code", arguments={"code": "print(persistent_var)", "timeout": 10}
            )

            data2 = json.loads(result2.content[0].text)
            if not data2.get("success"):
                print(f"Error in second execution: {data2.get('error')}")
                print(f"Stderr: {data2.get('stderr')}")
            assert data2["success"] is True
            # Note: May fail if sessions not properly linked - that's expected for now

    async def test_execute_code_with_packages(self, authenticated_client_session):
        """Test executing code that uses installed packages."""
        code = """
import pandas as pd
import numpy as np

df = pd.DataFrame({
    'A': np.array([1, 2, 3]),
    'B': np.array([4, 5, 6])
})
print(df.sum().to_dict())
"""

        async with authenticated_client_session() as session:
            await session.initialize()
            result = await session.call_tool(
                "execute_example_code",
                arguments={
                    "code": code,
                    "timeout": 30,  # Package imports may take time
                },
            )

        response_data = parse_tool_result(result)

        assert response_data["success"] is True
        assert "'A': 6" in response_data["stdout"] or '"A": 6' in response_data["stdout"]
        assert "'B': 15" in response_data["stdout"] or '"B": 15' in response_data["stdout"]


@pytest.mark.live
@pytest.mark.asyncio
class TestDataLakeAssets:
    """Test data lake asset resolution and manipulation."""

    async def test_inspect_asset_netcdf(self, authenticated_client_session):
        """Test inspecting a NetCDF data lake asset."""
        # Texas grid NetCDF file URL
        blob_url = "https://grid0eastus2.blob.core.windows.net/demo/texas_grid/texas_elec_base_network.nc"

        # Encode as base64 and wrap in type tags (required by asset resolution middleware)
        import base64

        artifact_id = base64.b64encode(blob_url.encode()).decode()
        qualified_name = f"<blob>{artifact_id}</blob>"

        async with authenticated_client_session() as session:
            await session.initialize()
            result = await session.call_tool(
                "inspect_asset",
                arguments={"asset": qualified_name},
            )

        response_text = result.content[0].text
        try:
            response_data = json.loads(response_text)
        except json.JSONDecodeError:
            setup_error_markers = [
                "az login",
                "please run 'az login'",
                "failed to resolve datalake asset",
                "authentication",
            ]
            if any(marker in response_text.lower() for marker in setup_error_markers):
                pytest.skip(f"Skipping live asset test due to environment/auth setup issue: {response_text}")
            raise

        # Verify successful execution
        assert response_data["success"] is True, f"Tool execution failed: {response_data.get('error')}"

        # Verify response structure
        assert "type" in response_data, "Response should contain 'type'"
        assert "value_summary" in response_data, "Response should contain 'value_summary'"
        assert "details" in response_data, "Response should contain 'details'"

        # Check that the asset was resolved (not just passed as a string)
        asset_type = response_data["type"]
        assert asset_type != "str", f"Asset should be resolved to an object, not a string. Got type: {asset_type}"

        # Log the results for inspection
        print("\n=== Data Lake Asset Inspection ===")
        print(f"Blob URL: {blob_url}")
        print(f"Artifact ID: {qualified_name}")
        print(f"Resolved Type: {asset_type}")
        print(f"Value Summary: {response_data['value_summary']}")
        print(f"Details: {response_data['details']}")


@pytest.mark.live
@pytest.mark.asyncio
class TestErrorHandling:
    """Test error handling in tool execution."""

    async def test_code_execution_error(self, authenticated_client_session):
        """Test that code errors are properly reported."""
        async with authenticated_client_session() as session:
            await session.initialize()
            result = await session.call_tool(
                "execute_example_code", arguments={"code": "raise ValueError('intentional error')", "timeout": 10}
            )

        response_data = parse_tool_result(result)

        assert response_data["success"] is False
        assert "ValueError" in response_data["stderr"] or "ValueError" in response_data.get("error", "")

    async def test_tool_error_in_code(self, authenticated_client_session):
        """Test that tool errors inside code execution are properly reported."""
        code = """
# increment_counter does counter + amount; passing a string raises TypeError
increment_counter(counter="not_a_number", amount=5)
"""
        async with authenticated_client_session() as session:
            await session.initialize()
            result = await session.call_tool("execute_example_code", arguments={"code": code, "timeout": 10})

        response_data = parse_tool_result(result)
        # The code should fail because the tool raises TypeError
        assert not response_data["success"]


if __name__ == "__main__":
    # Allow running tests directly
    pytest.main([__file__, "-v", "-s"])
