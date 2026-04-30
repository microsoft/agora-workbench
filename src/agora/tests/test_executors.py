"""Tests for base agent executors."""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from agent_framework import WorkflowContext

from agora.executors import (
    BaseLLMExecutor,
    SolutionHandlerExecutor,
    HelpHandlerExecutor,
    HelpRequest,
)
from agora.response_models import (
    AgentResponse,
    SolutionResponse,
    HelpResponse,
)


@pytest.fixture(autouse=True)
def _mock_mcp_registry():
    """Mock MCP registry so ToolDefinition construction passes without real servers."""
    mock_registry = MagicMock()
    mock_registry.has_server.return_value = True
    mock_registry.list_servers.return_value = {}
    with patch("tools.mcp.mcp_server_registry.get_mcp_registry", return_value=mock_registry):
        yield


class TestBaseLLMExecutor:
    """Test cases for BaseLLMExecutor."""

    @pytest.mark.unit
    def test_base_llm_executor_initialization(self, mock_chat_client):
        """Test BaseLLMExecutor initialization."""
        system_prompt = "Test system prompt"
        max_iterations = 10

        executor = BaseLLMExecutor(mock_chat_client, system_prompt, max_iterations)

        assert executor.chat_client == mock_chat_client
        assert executor.system_prompt == system_prompt
        assert executor.max_iterations == max_iterations
        assert executor.iteration == 0

    @pytest.mark.unit
    def test_base_llm_executor_initialization_with_tools(self, mock_chat_client):
        """Test BaseLLMExecutor initialization with tools."""
        system_prompt = "Test system prompt"
        max_iterations = 10
        mock_tool = MagicMock()

        executor = BaseLLMExecutor(mock_chat_client, system_prompt, max_iterations, tools=[mock_tool])

        assert len(executor.base_tools) == 1
        assert executor.base_tools[0] == mock_tool



class TestSolutionHandlerExecutor:
    """Test cases for SolutionHandlerExecutor."""

    @pytest.mark.unit
    def test_solution_handler_initialization(self):
        """Test SolutionHandlerExecutor initialization."""
        executor = SolutionHandlerExecutor()
        assert executor.id == "solution_handler"

    @pytest.mark.asyncio
    async def test_solution_handler_yields_solution(self):
        """Test solution handler yields solution output."""
        executor = SolutionHandlerExecutor()

        agent_response = AgentResponse(
            explanation="I have the solution", response=SolutionResponse(solution="The answer is 42")
        )

        mock_ctx = MagicMock(spec=WorkflowContext)
        mock_ctx.yield_output = AsyncMock()

        await executor.handle_solution(agent_response, mock_ctx)

        # Should yield solution
        mock_ctx.yield_output.assert_called_once()
        solution = mock_ctx.yield_output.call_args[0][0]
        assert isinstance(solution, SolutionResponse)
        assert solution.solution == "The answer is 42"


class TestHelpHandlerExecutor:
    """Test cases for HelpHandlerExecutor."""

    @pytest.mark.unit
    def test_help_handler_initialization(self, mock_chat_client):
        """Test HelpHandlerExecutor initialization."""
        llm_executor = BaseLLMExecutor(mock_chat_client, "Test", 10)
        executor = HelpHandlerExecutor(llm_executor)

        assert executor.llm_executor == llm_executor

    @pytest.mark.asyncio
    async def test_help_handler_requests_user_input(self, mock_chat_client):
        """Test help handler requests user input via request_info."""
        llm_executor = BaseLLMExecutor(mock_chat_client, "Test", 10)
        executor = HelpHandlerExecutor(llm_executor)

        agent_response = AgentResponse(
            explanation="Need user input", response=HelpResponse(question="What is your preference?")
        )

        mock_ctx = MagicMock(spec=WorkflowContext)
        mock_ctx.request_info = AsyncMock()

        await executor.handle_help(agent_response, mock_ctx)

        # Should request user input
        mock_ctx.request_info.assert_called_once()
        help_request = mock_ctx.request_info.call_args[0][0]
        assert isinstance(help_request, HelpRequest)
        assert help_request.question == "What is your preference?"
