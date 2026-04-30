"""Integration tests for AgoraAgent with dynamic tool loading."""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from .. import agent as _agent_module
from ..agent import AgoraAgent


@pytest.fixture
def test_domain_prompt():
    """Return a test domain prompt path (doesn't need to exist, we'll mock the renderer)."""
    return "test/domain_prompt.jinja"


class TestAgoraAgentIntegration:
    """Integration tests for complete AgoraAgent workflow."""

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_agent_initialization(self, test_domain_prompt, mock_environment_variables):
        """Test agent initializes correctly."""
        with (
            patch("agora.agent.AzureOpenAIChatClient") as mock_client_class,
            patch.object(_agent_module, "render_system_prompt", return_value="Test system prompt"),
        ):
            mock_client = MagicMock()
            mock_client_class.return_value = mock_client

            agent = AgoraAgent(
                domain_prompt_path=test_domain_prompt,
                llm="gpt-4o",
                max_iterations=10,
            )

            assert agent.llm_model == "gpt-4o"
            assert agent.max_iterations == 10

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_workflow_builds_successfully(self, test_domain_prompt, mock_environment_variables):
        """Test workflow builds without errors."""
        with (
            patch("agora.agent.AzureOpenAIChatClient") as mock_client_class,
            patch.object(_agent_module, "render_system_prompt", return_value="Test system prompt"),
        ):
            mock_client = MagicMock()
            mock_client.create_agent = MagicMock()
            mock_client_class.return_value = mock_client

            agent = AgoraAgent(
                domain_prompt_path=test_domain_prompt,
                llm="gpt-4o",
                max_iterations=10,
            )

            # Build workflow - should not raise
            workflow = agent._build_workflow()
            assert workflow is not None

    @pytest.mark.integration
    def test_agent_workflow_integration(self, test_domain_prompt, mock_environment_variables):
        """Test that agent workflow is properly built with tools."""
        with (
            patch("agora.agent.AzureOpenAIChatClient") as mock_client_class,
            patch.object(_agent_module, "render_system_prompt", return_value="Test system prompt"),
        ):
            mock_client = MagicMock()
            mock_client.create_agent = MagicMock(return_value=MagicMock())
            mock_client_class.return_value = mock_client

            agent = AgoraAgent(
                domain_prompt_path=test_domain_prompt,
                llm="gpt-4o",
                max_iterations=10,
            )

            # Build workflow to create executors
            workflow = agent._build_workflow()

            # Verify workflow is built
            assert workflow is not None

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_agent_close_calls_registry_aclose(self, mock_environment_variables):
        """Test that AgoraAgent.close() calls get_mcp_registry().aclose()."""
        with (
            patch("agora.agent.AzureOpenAIChatClient") as mock_client_class,
            patch.object(_agent_module, "render_system_prompt", return_value="Test system prompt"),
        ):
            mock_client = MagicMock()
            mock_client_class.return_value = mock_client

            agent = AgoraAgent(llm="gpt-4o", max_iterations=10)

            mock_registry = MagicMock()
            mock_registry.aclose = AsyncMock()

            with patch(
                "tools.mcp.mcp_server_registry.get_mcp_registry",
                return_value=mock_registry,
            ):
                await agent.close()

            mock_registry.aclose.assert_awaited_once()
