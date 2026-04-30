"""Tests for Azure AI Foundry tools integration."""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from dotenv import load_dotenv

from tools.foundry.foundry_client import (
    FoundryClientManager,
    get_foundry_client,
    reset_foundry_client,
)
from tools.foundry.foundry_adapter import (
    FoundryToolAdapter,
    get_foundry_adapter,
)
from tools.foundry.foundry_models import (
    FoundryAgentConfig,
    FoundryBuiltinTool,
    FoundryToolParameters,
    FoundryToolResult,
)
from code_execution import ToolDefinition, ToolParameter

# Load .env file for live tests
load_dotenv(Path(__file__).parents[3] / ".env")  # Load from project root


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def mock_credential():
    """Mock Azure credential."""
    with patch("tools.foundry.foundry_client.create_azure_credential") as mock:
        mock.return_value = MagicMock()
        yield mock


@pytest.fixture
def mock_ai_project_client():
    """Mock AIProjectClient."""
    with patch("tools.foundry.foundry_client.AIProjectClient") as mock:
        yield mock


@pytest.fixture
def sample_foundry_tool():
    """Sample Foundry tool definition as a FoundryBuiltinTool Pydantic model."""
    return FoundryBuiltinTool(
        name="bing_grounding",
        description="Search the web using Bing",
        parameters=FoundryToolParameters(
            properties={
                "query": {"type": "string", "description": "The search query"},
                "count": {"type": "integer", "description": "Number of results", "default": 10},
            },
            required=["query"],
        ),
        tool_class=MagicMock(),  # Mock the Azure SDK class
        requires_connection=False,
    )


@pytest.fixture(autouse=True)
def reset_singletons():
    """Reset global singletons before each test."""
    reset_foundry_client()
    yield
    reset_foundry_client()


# ============================================================================
# Unit Tests - FoundryClientManager
# ============================================================================


class TestFoundryClientManager:
    """Test cases for FoundryClientManager."""

    @pytest.mark.unit
    def test_loads_from_environment(self, mock_credential, mock_ai_project_client):
        """Test client reads config from environment variables."""
        with patch.dict(
            "os.environ",
            {
                "AZURE_AI_FOUNDRY_ENDPOINT": "https://test.services.ai.azure.com/",
            },
            clear=False,
        ):
            manager = FoundryClientManager()
            assert manager.endpoint == "https://test.services.ai.azure.com/"

    @pytest.mark.unit
    def test_requires_endpoint(self, mock_credential, mock_ai_project_client):
        """Test that constructing client without endpoint raises error."""
        # Clear all endpoint-related env vars to test the error case
        with patch.dict(
            "os.environ",
            {
                "AZURE_AI_FOUNDRY_ENDPOINT": "",
            },
            clear=False,
        ):
            with pytest.raises(ValueError, match="Must provide endpoint"):
                FoundryClientManager()

    @pytest.mark.unit
    def test_lazy_initialization(self, mock_credential, mock_ai_project_client):
        """Test that client and credential are lazily initialized and cached."""
        manager = FoundryClientManager(endpoint="https://test.services.ai.azure.com/")

        assert manager._client is None
        assert manager._credential is None

        # First access creates them
        _ = manager.client
        _ = manager.client  # Second access reuses

        mock_credential.assert_called_once()
        mock_ai_project_client.assert_called_once()

    @pytest.mark.unit
    def test_singleton_pattern(self, mock_credential, mock_ai_project_client):
        """Test that get_foundry_client returns singleton."""
        client1 = get_foundry_client(endpoint="https://test.services.ai.azure.com/")
        client2 = get_foundry_client()
        assert client1 is client2

    @pytest.mark.unit
    def test_agent_config_defaults(self, mock_credential, mock_ai_project_client):
        """Test that FoundryAgentConfig has sensible defaults."""
        manager = FoundryClientManager(endpoint="https://test.services.ai.azure.com/")

        assert manager.agent_config is not None
        assert manager.agent_config.model_deployment == "gpt-4o"
        assert "{tool_name}" in manager.agent_config.name_template
        assert "{tool_name}" in manager.agent_config.instructions_template

    @pytest.mark.unit
    def test_custom_agent_config(self, mock_credential, mock_ai_project_client):
        """Test that custom FoundryAgentConfig is used."""
        custom_config = FoundryAgentConfig(
            model_deployment="gpt-4-turbo",
            name_template="custom_agent_{tool_name}",
            instructions_template="Custom instructions for {tool_name}",
        )
        manager = FoundryClientManager(
            endpoint="https://test.services.ai.azure.com/",
            agent_config=custom_config,
        )

        assert manager.agent_config.model_deployment == "gpt-4-turbo"
        assert manager.agent_config.get_agent_name("test") == "custom_agent_test"
        assert manager.agent_config.get_instructions("test") == "Custom instructions for test"

    @pytest.mark.unit
    def test_get_tool_case_insensitive(self, mock_credential, mock_ai_project_client):
        """Test that get_tool is case-insensitive."""
        manager = FoundryClientManager(endpoint="https://test.services.ai.azure.com/")

        tool1 = manager.get_tool("bing_grounding")
        tool2 = manager.get_tool("BING_GROUNDING")
        tool3 = manager.get_tool("Bing_Grounding")

        assert tool1.name == tool2.name == tool3.name == "bing_grounding"

    @pytest.mark.unit
    def test_cached_agents(self, mock_credential, mock_ai_project_client):
        """Test that agents are cached and reused."""
        manager = FoundryClientManager(endpoint="https://test.services.ai.azure.com/")

        # Initially no cached agents
        assert len(manager._cached_agents) == 0

        # Simulate caching an agent
        manager._cached_agents["test_tool"] = "agent-123"
        assert "test_tool" in manager._cached_agents

        # Cleanup should remove it
        mock_agents_client = MagicMock()
        with patch.object(manager, "get_agents_client", return_value=mock_agents_client):
            manager.cleanup_cached_agents()

        assert len(manager._cached_agents) == 0
        mock_agents_client.delete_agent.assert_called_once_with("agent-123")

    @pytest.mark.unit
    def test_call_tool(self, mock_credential, mock_ai_project_client):
        """Test calling a tool using the agents client."""
        # Set up a mock agents client with the methods used by call_tool
        mock_agents_client = MagicMock()

        mock_agents_client.create_agent.return_value = MagicMock(id="agent-id")

        mock_threads_client = MagicMock()
        mock_threads_client.create.return_value = MagicMock(id="thread-id")
        mock_agents_client.threads = mock_threads_client

        mock_messages_client = MagicMock()
        mock_messages_client.create.return_value = MagicMock(id="message-id")
        # messages.list returns message objects with role and content attributes
        mock_content = MagicMock()
        mock_content.text = MagicMock(value="final-response")
        mock_message = MagicMock(role="ASSISTANT", content=[mock_content])
        mock_messages_client.list.return_value = [mock_message]
        mock_agents_client.messages = mock_messages_client

        mock_runs_client = MagicMock()
        mock_runs_client.create_and_process.return_value = MagicMock(id="run-id", status="succeeded")
        mock_agents_client.runs = mock_runs_client

        mock_agents_client.delete_agent.return_value = None

        manager = FoundryClientManager(endpoint="https://test.services.ai.azure.com/")

        # Patch get_agents_client so call_tool uses our mock agents client
        # Also patch create_tool_instance since call_tool needs a valid tool definition
        mock_tool_definition = MagicMock()
        with (
            patch.object(FoundryClientManager, "get_agents_client", return_value=mock_agents_client),
            patch.object(manager, "create_tool_instance", return_value=mock_tool_definition),
        ):
            result = manager.call_tool("test_tool", {"query": "test"})

        # Ensure the expected agent operations were invoked
        mock_agents_client.create_agent.assert_called_once()
        mock_agents_client.threads.create.assert_called_once()
        mock_agents_client.messages.create.assert_called_once()
        mock_agents_client.runs.create_and_process.assert_called_once()
        mock_agents_client.messages.list.assert_called_once()
        # Note: delete_agent is NOT called - agents are cached for reuse

        # Verify result is a FoundryToolResult
        assert isinstance(result, FoundryToolResult)
        assert result.success is True
        assert result.tool == "test_tool"


# ============================================================================
# Unit Tests - FoundryToolAdapter
# ============================================================================


class TestFoundryToolAdapter:
    """Test cases for FoundryToolAdapter."""

    @pytest.mark.unit
    def test_convert_foundry_tool_to_definition(self, mock_credential, mock_ai_project_client, sample_foundry_tool):
        """Test converting Foundry tool format to ToolDefinition."""
        client = FoundryClientManager(endpoint="https://test.services.ai.azure.com/")
        adapter = FoundryToolAdapter(client_manager=client)

        tool_def = adapter._convert_foundry_tool_to_definition(sample_foundry_tool)

        assert tool_def.name == "bing_grounding"
        assert tool_def.server_name == "foundry"
        assert len(tool_def.required_parameters) == 1
        assert tool_def.required_parameters[0].name == "query"
        assert len(tool_def.optional_parameters) == 1
        assert tool_def.optional_parameters[0].name == "count"

    @pytest.mark.unit
    def test_execute_tool_success(self, mock_credential, mock_ai_project_client):
        """Test successful tool execution returns FoundryToolResult."""
        mock_result = FoundryToolResult(
            success=True,
            result="The answer is 42",
            tool="test_tool",
            thread_id="thread-123",
            run_status="completed",
        )

        client = FoundryClientManager(endpoint="https://test.services.ai.azure.com/")
        adapter = FoundryToolAdapter(client_manager=client)

        with patch.object(client, "call_tool", return_value=mock_result):
            result = adapter.execute_tool("test_tool", {"param": "value"})

        assert isinstance(result, FoundryToolResult)
        assert result.success is True
        assert result.result == "The answer is 42"
        assert result.tool == "test_tool"

    @pytest.mark.unit
    def test_execute_tool_failure(self, mock_credential, mock_ai_project_client):
        """Test tool execution failure returns FoundryToolResult with error."""
        mock_result = FoundryToolResult(
            success=False,
            error="API Error",
            tool="test_tool",
        )

        client = FoundryClientManager(endpoint="https://test.services.ai.azure.com/")
        adapter = FoundryToolAdapter(client_manager=client)

        with patch.object(client, "call_tool", return_value=mock_result):
            result = adapter.execute_tool("test_tool", {"param": "value"})

        # Verify error handling
        assert isinstance(result, FoundryToolResult)
        assert result.success is False
        assert result.error is not None and "API Error" in result.error
        assert result.tool == "test_tool"


# ============================================================================
# Unit Tests - server_name for Foundry
# ============================================================================


class TestFoundryServerName:
    """Test server_name with Foundry type."""

    @pytest.mark.unit
    def test_foundry_server_name(self):
        """Test creating ToolDefinition with foundry server_name."""
        tool_def = ToolDefinition(
            name="bing_grounding",
            description="Search the web",
            required_parameters=[ToolParameter(name="query", type=str, description="Query")],
            optional_parameters=[],
            module="tools.foundry.foundry_adapter",
            server_name="foundry",
        )
        assert tool_def.server_name == "foundry"


# ============================================================================
# Live Tests (require Azure connection + .env)
# ============================================================================


def has_foundry_credentials() -> bool:
    """Check if Azure AI Foundry credentials are available."""
    return bool(os.getenv("AZURE_AI_FOUNDRY_ENDPOINT"))


skip_if_no_credentials = pytest.mark.skipif(
    not has_foundry_credentials(), reason="Set AZURE_AI_FOUNDRY_ENDPOINT in .env file"
)

skip_if_no_bing_connection = pytest.mark.skipif(
    not os.getenv("BING_GROUNDING_CONNECTION_ID"),
    reason="Set BING_GROUNDING_CONNECTION_ID in .env file",
)


@pytest.mark.live
class TestFoundryLive:
    """Live tests connecting to actual Azure AI Foundry.

    Run with: pytest -m live core/tests/tools/test_foundry_tools.py -v -s
    """

    @skip_if_no_credentials
    def test_client_connects(self):
        """Test connection and authentication to Azure AI Foundry."""
        client = get_foundry_client()
        assert client.endpoint is not None
        _ = client.credential  # Triggers auth
        print(f"✓ Connected to: {client.endpoint}")

    @skip_if_no_credentials
    def test_list_tools_live(self):
        """Test listing tools from workspace."""
        client = get_foundry_client()
        tools = client.list_builtin_tools

        print(f"\n✓ Found {len(tools)} tools:")
        for tool in tools[:5]:
            print(f"  - {tool.name}")

        assert isinstance(tools, list)
        assert all(isinstance(t, FoundryBuiltinTool) for t in tools)

    @skip_if_no_credentials
    def test_discover_and_convert_tools(self):
        """Test discovering tools and converting to ToolDefinitions."""
        adapter = get_foundry_adapter()
        tools = adapter.discover_tools()

        print(f"\n✓ Converted {len(tools)} tools to ToolDefinitions")

        assert all(isinstance(t, ToolDefinition) for t in tools)
        assert all(t.server_name == "foundry" for t in tools)

    @skip_if_no_credentials
    def test_full_workflow(self):
        """End-to-end: discover → select → execute."""
        adapter = get_foundry_adapter()

        tools = adapter.discover_tools()
        if not tools:
            pytest.skip("No tools available")

        tool = tools[0]
        print(f"\n✓ Selected: {tool.name}")

        # Build params based on tool definition
        params = {}
        for p in tool.required_parameters:
            params[p.name] = "test" if p.type is str else 1

        result = adapter.execute_tool(tool.name, params)
        print(f"✓ Executed: success={result.success}")

        assert isinstance(result, FoundryToolResult)
        assert result.tool == tool.name

    @skip_if_no_credentials
    def test_discover_builtin_azure_tools(self):
        """Test discovering built-in Azure AI Foundry tools.

        Azure AI Foundry provides built-in tools like:
        - bing_grounding: Web search via Bing
        - code_interpreter: Execute Python code
        - azure_ai_search: Search Azure AI Search indexes
        - file_search: Search uploaded files
        """
        adapter = get_foundry_adapter()
        tools = adapter.discover_tools()

        # Known built-in Azure AI Foundry tool names
        builtin_tool_names = {
            "bing_grounding",
            "code_interpreter",
            "azure_ai_search",
            "file_search",
            "microsoft_fabric",
            "sharepoint_grounding",
            "deep_research",
        }

        discovered_names = {t.name for t in tools}
        found_builtins = discovered_names & builtin_tool_names

        print(f"\n✓ Discovered {len(tools)} total tools")
        print(f"✓ Found {len(found_builtins)} built-in Azure tools:")
        for name in sorted(found_builtins):
            tool = next(t for t in tools if t.name == name)
            print(f"  - {name}: {tool.description[:60]}...")
            print(f"    Required: {[p.name for p in tool.required_parameters]}")
            print(f"    Optional: {[p.name for p in tool.optional_parameters]}")

        # At least one built-in tool should be available
        assert len(found_builtins) > 0, (
            f"Expected at least one built-in tool from {builtin_tool_names}, but found: {discovered_names}"
        )

    @skip_if_no_credentials
    def test_execute_deep_research(self):
        """Test executing the deep_research tool.

        Deep Research performs multi-step web research to answer complex questions.
        """
        adapter = get_foundry_adapter()

        # Verify deep_research is available
        tools = adapter.discover_tools()
        deep_research = next((t for t in tools if t.name == "deep_research"), None)

        if not deep_research:
            pytest.skip("deep_research tool not available in workspace")

        print("\n✓ Found deep_research tool")
        print(f"  Description: {deep_research.description[:100]}...")
        print(f"  Required params: {[p.name for p in deep_research.required_parameters]}")
        print(f"  Optional params: {[p.name for p in deep_research.optional_parameters]}")

        # Execute deep research with a test query
        result = adapter.execute_tool("deep_research", {"query": "What are the key features of Azure AI Foundry?"})

        print("\n✓ Execution completed")
        print(f"  Success: {result.success}")

        if result.success:
            result_str = str(result.result)
            print(f"  Result preview: {result_str[:500]}...")
        else:
            print(f"  Error: {result.error}")

        assert result.tool == "deep_research"
        assert result.success is True, f"deep_research failed: {result.error}"

    @skip_if_no_credentials
    def test_return_spec_on_discovered_tools(self):
        """Test that discovered tools have proper ReturnSpec defined.

        Verifies that all tools converted from Azure AI Foundry have:
        - A non-empty return_spec list
        - Each ReturnSpec has the expected fields (name, type, description)
        - Foundry tools return dict type
        """
        from code_execution import ReturnSpec

        adapter = get_foundry_adapter()
        tools = adapter.discover_tools()

        if not tools:
            pytest.skip("No tools available in workspace")

        print(f"\n✓ Checking return_spec on {len(tools)} discovered tools:")

        for tool in tools:
            print(f"\n  Tool: {tool.name}")

            # Verify return_spec exists and is a list
            assert hasattr(tool, "return_spec"), f"Tool {tool.name} missing return_spec"
            assert isinstance(tool.return_spec, list), f"Tool {tool.name} return_spec should be a list"
            assert len(tool.return_spec) > 0, f"Tool {tool.name} has empty return_spec"

            for rs in tool.return_spec:
                print(f"    - ReturnSpec: name={rs.name}, type={rs.type}")

                # Verify it's a proper ReturnSpec instance
                assert isinstance(rs, ReturnSpec), f"Expected ReturnSpec, got {type(rs)}"

                # Verify required fields
                assert rs.name is not None, "ReturnSpec name should not be None"
                assert rs.type is not None, "ReturnSpec type should not be None"

                # Foundry tools should return dict type
                assert rs.type is dict, f"Expected dict type for Foundry tool, got {rs.type}"

        print(f"\n✓ All {len(tools)} tools have valid return_spec")

    @skip_if_no_credentials
    @skip_if_no_bing_connection
    def test_cached_agents_live(self):
        """Test that agents are cached and reused across multiple tool calls.

        This live test verifies:
        1. First call creates an agent and caches it
        2. Second call reuses the cached agent (no new agent created)
        3. Cleanup removes the cached agent from Azure
        """
        client = get_foundry_client()

        # Ensure we start with no cached agents
        client.cleanup_cached_agents()
        assert len(client._cached_agents) == 0
        print("\n✓ Started with empty agent cache")

        # First call should create and cache an agent
        result1 = client.call_tool("bing_grounding", {"query": "test query 1"})
        print(f"✓ First call completed: success={result1.success}")

        if not result1.success:
            pytest.fail(f"First call failed: {result1.error}")

        assert "bing_grounding" in client._cached_agents
        cached_agent_id = client._cached_agents["bing_grounding"]
        print(f"✓ Agent cached: {cached_agent_id}")

        # Second call should reuse the cached agent
        result2 = client.call_tool("bing_grounding", {"query": "test query 2"})
        print(f"✓ Second call completed: success={result2.success}")

        # Verify the same agent was reused
        assert client._cached_agents["bing_grounding"] == cached_agent_id
        print("✓ Same agent was reused (cache hit)")

        # Cleanup should remove the agent
        client.cleanup_cached_agents()
        assert len(client._cached_agents) == 0
        print("✓ Agent cache cleaned up successfully")
