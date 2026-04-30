"""Shared pytest fixtures for AgoraAgentMAF tests."""

import os
from unittest.mock import MagicMock, AsyncMock, patch

import pytest
from agent_framework import Message

from code_execution import ToolDefinition, ToolParameter


@pytest.fixture
def mock_chat_client():
    """Mock MAF ChatClient for testing."""
    mock_client = MagicMock()
    return mock_client


@pytest.fixture
def mock_chat_agent():
    """Mock MAF Agent for testing."""
    mock_agent = MagicMock()
    mock_thread = MagicMock()
    mock_agent.create_session.return_value = mock_thread

    # Mock async run method
    mock_response = MagicMock()
    mock_response.text = (
        '{"explanation": "test reasoning", "response": {"type": "solution", "solution": "test solution"}}'
    )
    mock_agent.run = AsyncMock(return_value=mock_response)

    return mock_agent


@pytest.fixture
def sample_chat_messages():
    """Sample MAF Message list for testing."""
    return [
        Message(role="user", text="Test user message"),
        Message(role="assistant", text="Test AI response"),
    ]


@pytest.fixture
def mock_environment_variables():
    """Mock environment variables for testing."""
    env_vars = {
        "AZURE_OPENAI_ENDPOINT": "https://test.openai.azure.com/",
        "AOAI_SCOPE": "https://cognitiveservices.azure.com/.default",
        "API_VERSION": "2024-10-21",
    }

    with patch.dict(os.environ, env_vars, clear=False):
        yield env_vars


@pytest.fixture
def sample_resources():
    """Sample resources for tool retrieval testing."""
    return {
        "tools": [
            {
                "name": "solve_network",
                "description": "Solve power network optimization",
                "module": "test.tools.solvers",
            },
            {
                "name": "build_base_network",
                "description": "Build base network topology",
                "module": "test.tools.network",
            },
        ],
        "data_lake": [
            {
                "name": "texas_elec_s7.nc",
                "description": "Texas electricity network data",
            },
            {
                "name": "california_grid.csv",
                "description": "California grid data",
            },
        ],
        "libraries": [
            {
                "name": "pypsa",
                "description": "Power System Analysis library",
            },
            {
                "name": "pandas",
                "description": "Data manipulation library",
            },
        ],
    }


@pytest.fixture
def sample_tool():
    """Sample tool definition for testing."""
    return ToolDefinition(
        name="solve_network",
        description="Solve power network optimization",
        required_parameters=[ToolParameter(name="network_id", type=str, description="Network identifier")],
        optional_parameters=[ToolParameter(name="solver", type=str, description="Solver name", default="highs")],
        module="test.tools.solvers",
    )


@pytest.fixture
def another_tool():
    """Another sample tool definition for testing."""
    return ToolDefinition(
        name="build_network",
        description="Build base network topology",
        required_parameters=[ToolParameter(name="region", type=str, description="Region name")],
        optional_parameters=[],
        module="test.tools.network",
    )


@pytest.fixture
def empty_registry():
    """Empty tool registry for testing."""
    from code_execution import ToolRegistry

    return ToolRegistry()


@pytest.fixture
def sample_tool_registry(sample_tool, another_tool):
    """Sample tool registry with pre-registered tools."""
    from code_execution import ToolRegistry

    registry = ToolRegistry()
    registry.register_tool(sample_tool)
    registry.register_tool(another_tool)
    return registry
