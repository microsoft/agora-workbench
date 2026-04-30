"""
Tests for the Foundry MCP server, tool registry, and tool functions.

Unit tests (no markers) mock the Azure AI Agents SDK and run offline.
Live integration tests require a running Foundry server at localhost:8003
and valid Azure credentials (``az login``).

Start the server with:
    docker compose -f code_execution/docker/docker-compose.yml --env-file .env up foundry-server

Run unit tests:
    uv run pytest domains/tests/test_foundry_server.py -v -m "not live"

Run live tests:
    uv run pytest domains/tests/test_foundry_server.py -v -m live

Run all:
    uv run pytest domains/tests/test_foundry_server.py -v
"""

import json
import logging
import os
from unittest.mock import MagicMock, patch

import pytest

from domains.foundry.server.tool_registry import create_foundry_tool_registry
from domains.foundry.server import foundry_tools

logging.getLogger("httpx").setLevel(logging.WARNING)

FOUNDRY_URL = "http://localhost:8003/mcp"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def parse_tool_result(result):
    """Parse MCP tool call result and extract the response data."""
    assert len(result.content) > 0
    response_text = result.content[0].text
    return json.loads(response_text)


def _make_mock_agents_client(response_text="mock response"):
    """Build a fully-wired mock AgentsClient that returns *response_text*."""
    client = MagicMock()

    mock_agent = MagicMock()
    mock_agent.id = "agent-123"
    client.create_agent.return_value = mock_agent

    mock_thread = MagicMock()
    mock_thread.id = "thread-456"
    client.threads.create.return_value = mock_thread

    client.messages.create.return_value = None

    mock_run = MagicMock()
    mock_run.status = "completed"
    mock_run.id = "run-789"
    client.runs.create_and_process.return_value = mock_run

    mock_content = MagicMock()
    mock_content.text = MagicMock()
    mock_content.text.value = response_text

    mock_msg = MagicMock()
    mock_msg.role = "ASSISTANT"
    mock_msg.content = [mock_content]

    client.messages.list.return_value = [mock_msg]

    return client


@pytest.fixture(autouse=True)
def _clear_caches():
    """Reset module-level caches between tests.

    Seeds ``_last_token`` with the current env value so tests that directly
    inject a mock client into ``_cached_clients["client"]`` won't trigger an
    unexpected token-mismatch rebuild.
    """
    foundry_tools._cached_clients.clear()
    foundry_tools._cached_agents.clear()
    foundry_tools._cached_clients["_last_token"] = os.environ.get("USER_ASSERTION_TOKEN", "")
    yield
    foundry_tools._cached_clients.clear()
    foundry_tools._cached_agents.clear()


# ===========================================================================
# Unit Tests (offline, mocked)
# ===========================================================================

EXPECTED_TOOLS = [
    "bing_grounding",
    "deep_research",
]

ALL_FOUNDRY_TOOL_FUNCTIONS = [
    "bing_grounding",
    "code_interpreter",
    "file_search",
    "azure_ai_search",
    "microsoft_fabric",
    "sharepoint_grounding",
    "deep_research",
]


# ---------------------------------------------------------------------------
# Tool Registry
# ---------------------------------------------------------------------------


class TestFoundryToolRegistry:
    """Tests for create_foundry_tool_registry()."""

    def test_tool_names_and_count(self):
        registry = create_foundry_tool_registry()
        names = sorted(t.name for t in registry.tools)
        assert names == sorted(EXPECTED_TOOLS)

    def test_tool_properties(self):
        """Every tool has correct module, server_name, query param, and return spec."""
        registry = create_foundry_tool_registry()
        for tool in registry.tools:
            assert tool.server_name == "foundry"
            assert tool.module == "domains.foundry.server.foundry_tools"
            assert "query" in [p.name for p in tool.required_parameters], tool.name
            assert tool.return_spec[0].name == "result"
            assert tool.return_spec[0].type is str


# ---------------------------------------------------------------------------
# _get_agents_client
# ---------------------------------------------------------------------------


class TestGetAgentsClient:
    """Tests for _get_agents_client()."""

    def test_raises_without_endpoint(self, monkeypatch):
        monkeypatch.delenv("AZURE_AI_FOUNDRY_ENDPOINT", raising=False)
        with pytest.raises(RuntimeError, match="AZURE_AI_FOUNDRY_ENDPOINT"):
            foundry_tools._get_agents_client()

    @patch("domains.foundry.server.foundry_tools.AgentsClient", create=True)
    def test_creates_and_caches_client(self, mock_cls, monkeypatch):
        monkeypatch.setenv("AZURE_AI_FOUNDRY_ENDPOINT", "https://test.services.ai.azure.com")
        with patch.dict(
            "sys.modules",
            {"azure.ai.agents": MagicMock(), "azure.identity": MagicMock()},
        ):
            foundry_tools._get_agents_client()
            assert "client" in foundry_tools._cached_clients

    def test_returns_cached_client(self, monkeypatch):
        monkeypatch.setenv("USER_ASSERTION_TOKEN", "same-token")
        mock_client = MagicMock()
        foundry_tools._cached_clients["client"] = mock_client
        foundry_tools._cached_clients["_last_token"] = "same-token"
        assert foundry_tools._get_agents_client() is mock_client

    @patch("domains.foundry.server.foundry_tools.AgentsClient", create=True)
    def test_rebuilds_client_on_token_change(self, mock_cls, monkeypatch):
        """Client is rebuilt when USER_ASSERTION_TOKEN changes in the env."""
        monkeypatch.setenv("AZURE_AI_FOUNDRY_ENDPOINT", "https://test.services.ai.azure.com")
        monkeypatch.setenv("USER_ASSERTION_TOKEN", "old-token")

        mock_obo_cls = MagicMock()
        mock_obo_cls.return_value._credential = MagicMock()
        foundry_tools._cached_clients["_obo_class"] = mock_obo_cls

        # First call — builds and caches with "old-token"
        foundry_tools._get_agents_client()
        assert foundry_tools._cached_clients["_last_token"] == "old-token"

        # Simulate token refresh preamble updating os.environ
        monkeypatch.setenv("USER_ASSERTION_TOKEN", "new-token")
        foundry_tools._get_agents_client()

        assert foundry_tools._cached_clients["_last_token"] == "new-token"
        # Verify the credential provider was rebuilt with the refreshed token
        assert mock_obo_cls.call_count == 2
        assert mock_obo_cls.call_args_list[-1] == ((), {"user_assertion": "new-token"})


# ---------------------------------------------------------------------------
# _call_foundry_tool
# ---------------------------------------------------------------------------


class TestCallFoundryTool:
    """Tests for the _call_foundry_tool() helper."""

    def test_creates_agent_and_returns_response(self):
        mock_client = _make_mock_agents_client("Bing says hello")
        foundry_tools._cached_clients["client"] = mock_client

        result = foundry_tools._call_foundry_tool("bing_grounding", "test query", [MagicMock()])

        assert result == "Bing says hello"
        mock_client.create_agent.assert_called_once()
        mock_client.runs.create_and_process.assert_called_once()

    def test_caches_agent_on_second_call(self):
        mock_client = _make_mock_agents_client("response")
        foundry_tools._cached_clients["client"] = mock_client

        foundry_tools._call_foundry_tool("test_tool", "q1", [MagicMock()])
        foundry_tools._call_foundry_tool("test_tool", "q2", [MagicMock()])

        assert mock_client.create_agent.call_count == 1
        assert mock_client.threads.create.call_count == 2

    def test_returns_fallback_when_no_content(self):
        mock_client = _make_mock_agents_client("response")
        mock_client.messages.list.return_value = []
        mock_client.run_steps.list.return_value = []
        foundry_tools._cached_clients["client"] = mock_client

        result = foundry_tools._call_foundry_tool("empty_tool", "q", [MagicMock()])
        assert "no content" in result.lower()

    def test_extracts_code_interpreter_output(self):
        mock_client = _make_mock_agents_client("response")
        mock_client.messages.list.return_value = []

        mock_output = MagicMock()
        mock_output.logs = "42"
        mock_ci = MagicMock(outputs=[mock_output])
        mock_tool_call = MagicMock(code_interpreter=mock_ci, deep_research=None)

        mock_details = MagicMock(tool_calls=[mock_tool_call])
        del mock_details.message_creation
        mock_step = MagicMock(step_details=mock_details)

        mock_client.run_steps.list.return_value = [mock_step]
        foundry_tools._cached_clients["client"] = mock_client

        result = foundry_tools._call_foundry_tool("ci_tool", "compute 6*7", [MagicMock()])
        assert "42" in result


# ---------------------------------------------------------------------------
# Individual Tool Functions (parametrized)
# ---------------------------------------------------------------------------


# Tools that require env vars to be set before calling
_TOOLS_REQUIRING_ENV = [
    ("bing_grounding", "BING_GROUNDING_CONNECTION_ID"),
    ("azure_ai_search", "AZURE_AI_SEARCH_CONNECTION_ID"),
    ("deep_research", "BING_GROUNDING_CONNECTION_ID"),
    ("microsoft_fabric", "MICROSOFT_FABRIC_CONNECTION_ID"),
    ("sharepoint_grounding", "SHAREPOINT_CONNECTION_ID"),
]


class TestToolFunctions:
    """Tests that each tool function delegates to _call_foundry_tool correctly."""

    @pytest.mark.parametrize("tool_name,env_var", _TOOLS_REQUIRING_ENV)
    def test_raises_without_required_env(self, tool_name, env_var, monkeypatch):
        """Tools that need connection IDs raise RuntimeError when unset."""
        # Clear all potentially required env vars
        for _, var in _TOOLS_REQUIRING_ENV:
            monkeypatch.delenv(var, raising=False)
        monkeypatch.delenv("AZURE_AI_SEARCH_INDEX_NAME", raising=False)
        monkeypatch.delenv("DEEP_RESEARCH_MODEL_DEPLOYMENT_NAME", raising=False)

        func = getattr(foundry_tools, tool_name)
        with pytest.raises(RuntimeError, match=env_var):
            func("test")

    @patch.object(foundry_tools, "_call_foundry_tool", return_value="ok")
    def test_all_tools_delegate_to_call_foundry_tool(self, mock_call, monkeypatch):
        """Every tool function delegates to _call_foundry_tool with the correct name."""
        # Set all env vars so nothing raises
        conn = "/subscriptions/s/resourceGroups/r/providers/Microsoft.CognitiveServices/accounts/a/projects/p/connections/c"
        monkeypatch.setenv("BING_GROUNDING_CONNECTION_ID", conn)
        monkeypatch.setenv("AZURE_AI_SEARCH_CONNECTION_ID", conn)
        monkeypatch.setenv("AZURE_AI_SEARCH_INDEX_NAME", "idx")
        monkeypatch.setenv("DEEP_RESEARCH_MODEL_DEPLOYMENT_NAME", "model")
        monkeypatch.setenv("MICROSOFT_FABRIC_CONNECTION_ID", conn)
        monkeypatch.setenv("SHAREPOINT_CONNECTION_ID", conn)

        for tool_name in ALL_FOUNDRY_TOOL_FUNCTIONS:
            mock_call.reset_mock()
            func = getattr(foundry_tools, tool_name)
            result = func("test query")
            assert result == "ok", f"{tool_name} did not return expected value"
            assert mock_call.call_args[0][0] == tool_name
            assert mock_call.call_args[0][1] == "test query"


# ---------------------------------------------------------------------------
# Foundry Server Config
# ---------------------------------------------------------------------------


class TestFoundryServerConfig:
    def test_config_properties(self):
        from domains.foundry.server.foundry_server import create_foundry_config

        config = create_foundry_config()
        assert config.name == "foundry"
        assert config.type == "uv"
        assert config.auto_build is True
        assert "azure-ai-agents" in config.dependency_file
        assert "azure-identity" in config.dependency_file


# ===========================================================================
# Live Integration Tests (require running server + Azure credentials)
# ===========================================================================


# ---------------------------------------------------------------------------
# Connection & Tool Discovery
# ---------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.asyncio
class TestFoundryServerConnection:
    """Test basic connectivity and tool listing."""

    async def test_server_connection(self, authenticated_client_session):
        """Can connect to the Foundry server and list tools."""
        async with authenticated_client_session(url=FOUNDRY_URL) as session:
            await session.initialize()
            response = await session.list_tools()

        tool_names = [t.name for t in response.tools]
        assert "execute_foundry_code" in tool_names
        assert "bing_grounding" in tool_names

    async def test_all_foundry_tools_registered(self, authenticated_client_session):
        """All currently enabled Foundry registry tools are exposed via MCP."""
        async with authenticated_client_session(url=FOUNDRY_URL) as session:
            await session.initialize()
            response = await session.list_tools()

        tool_names = [t.name for t in response.tools]
        expected = [t.name for t in create_foundry_tool_registry().tools]
        for name in expected:
            assert name in tool_names, f"Missing tool: {name}"

    async def test_tools_have_query_input_schema(self, authenticated_client_session):
        """Each Foundry tool accepts a 'query' string parameter."""
        async with authenticated_client_session(url=FOUNDRY_URL) as session:
            await session.initialize()
            response = await session.list_tools()

        foundry_tools = [
            t
            for t in response.tools
            if t.name
            in {
                "bing_grounding",
                "code_interpreter",
                "file_search",
                "azure_ai_search",
                "deep_research",
                "microsoft_fabric",
                "sharepoint_grounding",
            }
        ]

        for tool in foundry_tools:
            props = tool.inputSchema.get("properties", {})
            assert "query" in props, f"{tool.name} missing 'query' in inputSchema"

    async def test_session_management_tools_present(self, authenticated_client_session):
        """Session management tools (list/get/close) are available."""
        async with authenticated_client_session(url=FOUNDRY_URL) as session:
            await session.initialize()
            response = await session.list_tools()

        tool_names = [t.name for t in response.tools]
        assert "list_sessions" in tool_names
        assert "get_session_info" in tool_names
        assert "close_session" in tool_names


# ---------------------------------------------------------------------------
# Bing Grounding (full round-trip)
# ---------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.asyncio
class TestBingGroundingLive:
    """Live tests for bing_grounding tool via MCP."""

    async def test_bing_grounding_returns_results(self, authenticated_client_session):
        """bing_grounding returns a non-empty result for a web search."""
        async with authenticated_client_session(url=FOUNDRY_URL) as session:
            await session.initialize()
            result = await session.call_tool(
                "bing_grounding",
                arguments={"query": "What is the capital of France?"},
            )

        assert not result.isError, f"Tool returned error: {result.content}"
        data = parse_tool_result(result)
        assert data.get("success") is True
        assert data.get("result")  # non-empty string
        assert "Paris" in data["result"] or "paris" in data["result"].lower()

    async def test_bing_grounding_handles_special_characters(self, authenticated_client_session):
        """Queries with special chars (quotes, ampersands) work."""
        async with authenticated_client_session(url=FOUNDRY_URL) as session:
            await session.initialize()
            result = await session.call_tool(
                "bing_grounding",
                arguments={"query": 'best "synthetic power grid" datasets & benchmarks'},
            )

        assert not result.isError
        data = parse_tool_result(result)
        assert data.get("success") is True


# ---------------------------------------------------------------------------
# Code Execution (execute_foundry_code)
# ---------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.asyncio
class TestCodeExecutionLive:
    """Live tests for generic code execution on the Foundry server."""

    async def test_execute_with_imports(self, authenticated_client_session):
        """Can import standard library modules."""
        async with authenticated_client_session(url=FOUNDRY_URL) as session:
            await session.initialize()
            result = await session.call_tool(
                "execute_foundry_code",
                arguments={"code": "import json; print(json.dumps({'key': 'value'}))"},
            )

        assert not result.isError
        data = parse_tool_result(result)
        assert "key" in data.get("stdout", "")

    async def test_azure_sdk_available_in_kernel(self, authenticated_client_session):
        """The kernel environment has azure-ai-agents installed."""
        async with authenticated_client_session(url=FOUNDRY_URL) as session:
            await session.initialize()
            result = await session.call_tool(
                "execute_foundry_code",
                arguments={"code": "from azure.ai.agents import AgentsClient; print('azure-ai-agents OK')"},
            )

        assert not result.isError
        data = parse_tool_result(result)
        assert "OK" in data.get("stdout", "")


# ---------------------------------------------------------------------------
# Session Management
# ---------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.asyncio
class TestSessionManagementLive:
    """Live tests for session listing and cleanup."""

    async def test_list_sessions(self, authenticated_client_session):
        """list_sessions returns without error."""
        async with authenticated_client_session(url=FOUNDRY_URL) as session:
            await session.initialize()
            result = await session.call_tool(
                "list_sessions",
                arguments={"summary_only": True},
            )

        assert not result.isError
