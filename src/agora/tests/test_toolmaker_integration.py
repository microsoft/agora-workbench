"""Tests for ToolMaker integration with AgoraAgent.

Covers:
- Parameter storage (enable_toolmaker, toolmaker_llm)
- Workflow building with/without toolmaker enabled
- End-to-end flow: agent asks user for repo URL, then produces solution
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from .. import agent as _agent_module
from ..agent import AgoraAgent
from agora.response_models import SolutionResponse


def _make_result(outputs=None, pending=None):
    """Create a mock WorkflowRunResult."""
    result = MagicMock()
    result.get_outputs.return_value = outputs or []
    result.get_request_info_events.return_value = pending or []
    return result


def _make_request_event(request_id, question, context=""):
    """Create a mock request-info event."""
    event = MagicMock()
    event.request_id = request_id
    event.data = MagicMock()
    event.data.question = question
    event.data.context = context
    return event


@pytest.fixture
def toolmaker_agent(mock_environment_variables):
    """Create an AgoraAgent with enable_toolmaker=True."""
    with (
        patch("agora.agent.AzureOpenAIChatClient") as mock_client_class,
        patch.object(_agent_module, "render_system_prompt", return_value="prompt"),
    ):
        mock_client_class.return_value = MagicMock()
        yield AgoraAgent(llm="gpt-4o", max_iterations=5, enable_toolmaker=True)


class TestToolMakerParams:
    """Test enable_toolmaker parameter storage and defaults."""

    @pytest.mark.unit
    def test_toolmaker_disabled_by_default(self, mock_environment_variables):
        """Default agent does not have toolmaker enabled."""
        with (
            patch("agora.agent.AzureOpenAIChatClient") as mock_client_class,
            patch.object(_agent_module, "render_system_prompt", return_value="prompt"),
        ):
            mock_client_class.return_value = MagicMock()
            agent = AgoraAgent(llm="gpt-4o", max_iterations=5)
            assert agent.enable_toolmaker is False

    @pytest.mark.unit
    def test_toolmaker_enabled_stores_params(self, mock_environment_variables):
        """enable_toolmaker and toolmaker_llm are stored correctly."""
        with (
            patch("agora.agent.AzureOpenAIChatClient") as mock_client_class,
            patch.object(_agent_module, "render_system_prompt", return_value="prompt"),
        ):
            mock_client_class.return_value = MagicMock()
            agent = AgoraAgent(
                llm="gpt-4o",
                max_iterations=5,
                enable_toolmaker=True,
                toolmaker_llm="gpt-4o-mini",
            )
            assert agent.enable_toolmaker is True
            assert agent.toolmaker_llm == "gpt-4o-mini"

    @pytest.mark.unit
    def test_toolmaker_llm_defaults_to_agent_llm(self, mock_environment_variables):
        """toolmaker_llm defaults to the agent's own llm."""
        with (
            patch("agora.agent.AzureOpenAIChatClient") as mock_client_class,
            patch.object(_agent_module, "render_system_prompt", return_value="prompt"),
        ):
            mock_client_class.return_value = MagicMock()
            agent = AgoraAgent(llm="gpt-4o", max_iterations=5, enable_toolmaker=True)
            assert agent.toolmaker_llm == "gpt-4o"


class TestToolMakerWorkflow:
    """Test that enable_toolmaker wires create_tool_from_repo into the workflow."""

    @pytest.mark.unit
    def test_toolmaker_enabled_builds_workflow(self, mock_environment_variables):
        """Workflow builds successfully when enable_toolmaker=True."""
        with (
            patch("agora.agent.AzureOpenAIChatClient") as mock_client_class,
            patch.object(_agent_module, "render_system_prompt", return_value="prompt"),
        ):
            mock_client_class.return_value = MagicMock()
            agent = AgoraAgent(llm="gpt-4o", max_iterations=5, enable_toolmaker=True)
            workflow = agent._build_workflow()
            assert workflow is not None

    @pytest.mark.unit
    def test_toolmaker_disabled_builds_workflow(self, mock_environment_variables):
        """Workflow builds successfully when enable_toolmaker=False."""
        with (
            patch("agora.agent.AzureOpenAIChatClient") as mock_client_class,
            patch.object(_agent_module, "render_system_prompt", return_value="prompt"),
        ):
            mock_client_class.return_value = MagicMock()
            agent = AgoraAgent(llm="gpt-4o", max_iterations=5, enable_toolmaker=False)
            workflow = agent._build_workflow()
            assert workflow is not None


class TestToolMakerEndToEnd:
    """End-to-end tests for toolmaker agent interaction flow.

    These tests mock the workflow at the run() level (same pattern as test_agent_go.py)
    to verify the expected multi-round interaction:

    1. User asks for something that needs a tool
    2. Agent asks user for a repo URL (HelpResponse → request-info)
    3. User provides the URL
    4. Agent creates the tool and returns a solution
    """

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_agent_asks_for_repo_url_then_solves(self, toolmaker_agent):
        """When no tool exists, agent asks for repo URL, user provides it, agent solves."""
        # Round 1: Agent asks for repo URL via HelpResponse
        req_event = _make_request_event(
            "req-1",
            "I don't have a tool for Roman numeral conversion. "
            "Could you provide a GitHub repository URL that has this functionality?",
        )
        round1 = _make_result(pending=[req_event])

        # Round 2: After user provides URL, agent creates tool and returns solution
        solution = SolutionResponse(
            action="solution",
            solution="Tool 'to_roman' created from https://github.com/zopefoundation/roman. Converting 42 gives: XLII",
        )
        round2 = _make_result(outputs=[solution])

        mock_workflow = MagicMock()
        mock_workflow.run = AsyncMock(side_effect=[round1, round2])
        toolmaker_agent._build_workflow = AsyncMock(return_value=mock_workflow)

        # Simulate user providing the repo URL when asked
        input_handler = AsyncMock(return_value="https://github.com/zopefoundation/roman")

        msg = await toolmaker_agent.go(
            "Convert 42 to Roman numerals",
            input_handler=input_handler,
        )

        # Agent should have asked the user for the URL
        input_handler.assert_awaited_once()
        question_asked = input_handler.call_args[0][0]
        assert "repo" in question_asked.lower() or "url" in question_asked.lower()

        # Final answer should contain the solution
        assert "XLII" in msg.text

        # Workflow ran twice: initial prompt, then resume with user's URL
        assert mock_workflow.run.await_count == 2
        mock_workflow.run.assert_any_await("Convert 42 to Roman numerals")
        mock_workflow.run.assert_awaited_with(responses={"req-1": "https://github.com/zopefoundation/roman"})

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_agent_solves_directly_when_tool_exists(self, toolmaker_agent):
        """When a matching tool already exists, agent solves without asking for a URL."""
        solution = SolutionResponse(
            action="solution",
            solution="The optimal power flow solution converged.",
        )
        result = _make_result(outputs=[solution])

        mock_workflow = MagicMock()
        mock_workflow.run = AsyncMock(return_value=result)
        toolmaker_agent._build_workflow = AsyncMock(return_value=mock_workflow)

        msg = await toolmaker_agent.go("Run optimal power flow")

        assert msg.text == "The optimal power flow solution converged."
        # Only one run — no help request needed
        mock_workflow.run.assert_awaited_once_with("Run optimal power flow")

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_agent_handles_multiple_help_rounds(self, toolmaker_agent):
        """Agent can go through multiple help rounds (e.g., ask for URL, then clarify)."""
        # Round 1: Ask for repo URL
        req1 = _make_request_event("req-1", "Which GitHub repo should I use?")
        round1 = _make_result(pending=[req1])

        # Round 2: Ask for clarification about tool name
        req2 = _make_request_event("req-2", "What should the tool function be named?")
        round2 = _make_result(pending=[req2])

        # Round 3: Solution
        solution = SolutionResponse(action="solution", solution="Tool 'humanize_number' created.")
        round3 = _make_result(outputs=[solution])

        mock_workflow = MagicMock()
        mock_workflow.run = AsyncMock(side_effect=[round1, round2, round3])
        toolmaker_agent._build_workflow = AsyncMock(return_value=mock_workflow)

        answers = iter(
            [
                "https://github.com/jmoiron/humanize",
                "humanize_number",
            ]
        )
        input_handler = AsyncMock(side_effect=lambda q, c="": next(answers))

        msg = await toolmaker_agent.go("I need a tool to humanize numbers", input_handler=input_handler)

        assert msg.text == "Tool 'humanize_number' created."
        assert mock_workflow.run.await_count == 3
        assert input_handler.await_count == 2
