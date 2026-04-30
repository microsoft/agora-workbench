"""Tests for autopilot mode in PlanThenExecuteAgent.

Autopilot mode disables all user interaction: help handlers auto-resolve
instead of calling ``ctx.request_info``, and prompts instruct the LLM to
never emit HelpResponses.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agent_framework import WorkflowContext

from agent_bot.plan_then_execute.executors import (
    HelpHandlerExecutor,
    PlanningHelpHandlerExecutor,
    PresentationHelpHandlerExecutor,
    BaseLLMExecutor,
)
from agent_bot.plan_then_execute.plan import Plan
from agent_bot.plan_then_execute.response_models import (
    AgentResponse,
    HelpResponse,
    SolutionResponse,
)
from agent_bot.plan_then_execute.prompts.renderer import (
    render_planning_prompt,
    render_execution_prompt,
    render_presentation_prompt,
)
from .. import agent as _agent_module
from ..agent import PlanThenExecuteAgent


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _mock_mcp_registry():
    mock_registry = MagicMock()
    mock_registry.has_server.return_value = True
    mock_registry.list_servers.return_value = {}
    with patch("tools.mcp.mcp_server_registry.get_mcp_registry", return_value=mock_registry):
        yield


@pytest.fixture
def autopilot_agent(mock_environment_variables):
    """Create a PlanThenExecuteAgent with autopilot=True."""
    with (
        patch("agent_bot.plan_then_execute.agent.AzureOpenAIChatClient") as mock_client_class,
        patch.object(_agent_module, "render_planning_prompt", return_value="Planning prompt"),
        patch.object(_agent_module, "render_execution_prompt", return_value="Execution prompt"),
        patch.object(_agent_module, "render_presentation_prompt", return_value="Presentation prompt"),
    ):
        mock_client_class.return_value = MagicMock()
        yield PlanThenExecuteAgent(llm="gpt-4o", autopilot=True)


def _make_result(outputs=None, pending=None):
    result = MagicMock()
    result.get_outputs.return_value = outputs or []
    result.get_request_info_events.return_value = pending or []
    return result


def _make_request_event(request_id, question, context=""):
    event = MagicMock()
    event.request_id = request_id
    event.data = MagicMock()
    event.data.question = question
    event.data.context = context
    return event


# ── Prompt rendering tests ───────────────────────────────────────────────


class TestAutopilotPrompts:
    """Verify autopilot flag modifies rendered prompts."""

    @pytest.mark.unit
    def test_planning_prompt_default_has_help(self):
        prompt = render_planning_prompt()
        assert "HelpResponse" in prompt
        assert "ask for confirmation" in prompt

    @pytest.mark.unit
    def test_planning_prompt_autopilot_no_help(self):
        prompt = render_planning_prompt(autopilot=True)
        assert "Do NOT use HelpResponse" in prompt
        assert "autonomously" in prompt

    @pytest.mark.unit
    def test_execution_prompt_default_has_help(self):
        prompt = render_execution_prompt()
        assert "HelpResponse" in prompt

    @pytest.mark.unit
    def test_execution_prompt_autopilot_no_help(self):
        prompt = render_execution_prompt(autopilot=True)
        assert "Do NOT use HelpResponse" in prompt
        assert "best judgment" in prompt

    @pytest.mark.unit
    def test_presentation_prompt_default_has_help(self):
        prompt = render_presentation_prompt()
        assert "HelpResponse" in prompt
        assert "ask if the user is satisfied" in prompt

    @pytest.mark.unit
    def test_presentation_prompt_autopilot_solution_response(self):
        prompt = render_presentation_prompt(autopilot=True)
        assert "SolutionResponse" in prompt
        assert "Do NOT use HelpResponse" in prompt


# ── Executor autopilot tests ─────────────────────────────────────────────


class TestHelpHandlerAutopilot:
    """HelpHandlerExecutor auto-resolves in autopilot mode."""

    @pytest.mark.asyncio
    async def test_autopilot_sends_message_not_request_info(self, mock_chat_client):
        llm_executor = BaseLLMExecutor(mock_chat_client, "Test", 10)
        executor = HelpHandlerExecutor(llm_executor, autopilot=True)

        agent_response = AgentResponse(
            explanation="Need help",
            response=HelpResponse(question="Which dataset?"),
        )

        mock_ctx = MagicMock(spec=WorkflowContext)
        mock_ctx.send_message = AsyncMock()
        mock_ctx.request_info = AsyncMock()

        await executor.handle_help(agent_response, mock_ctx)

        mock_ctx.send_message.assert_called_once()
        mock_ctx.request_info.assert_not_called()
        assert "best judgment" in mock_ctx.send_message.call_args[0][0]

    @pytest.mark.asyncio
    async def test_non_autopilot_uses_request_info(self, mock_chat_client):
        llm_executor = BaseLLMExecutor(mock_chat_client, "Test", 10)
        executor = HelpHandlerExecutor(llm_executor, autopilot=False)

        agent_response = AgentResponse(
            explanation="Need help",
            response=HelpResponse(question="Which dataset?"),
        )

        mock_ctx = MagicMock(spec=WorkflowContext)
        mock_ctx.send_message = AsyncMock()
        mock_ctx.request_info = AsyncMock()

        await executor.handle_help(agent_response, mock_ctx)

        mock_ctx.request_info.assert_called_once()
        mock_ctx.send_message.assert_not_called()


class TestPlanningHelpHandlerAutopilot:
    """PlanningHelpHandlerExecutor auto-approves in autopilot mode."""

    @pytest.mark.asyncio
    async def test_autopilot_auto_approves(self, mock_chat_client):
        llm_executor = BaseLLMExecutor(mock_chat_client, "Test", 10)
        plan = Plan()
        plan.add_step("Step 1")
        executor = PlanningHelpHandlerExecutor(llm_executor, plan, autopilot=True)

        agent_response = AgentResponse(
            explanation="Here is the plan",
            response=HelpResponse(question="Does this look good?"),
        )

        mock_ctx = MagicMock(spec=WorkflowContext)
        mock_ctx.send_message = AsyncMock()
        mock_ctx.request_info = AsyncMock()

        await executor.handle_help(agent_response, mock_ctx)

        mock_ctx.send_message.assert_called_once()
        mock_ctx.request_info.assert_not_called()
        assert "Finalize" in mock_ctx.send_message.call_args[0][0]

    @pytest.mark.asyncio
    async def test_non_autopilot_requests_info(self, mock_chat_client):
        llm_executor = BaseLLMExecutor(mock_chat_client, "Test", 10)
        plan = Plan()
        executor = PlanningHelpHandlerExecutor(llm_executor, plan, autopilot=False)

        agent_response = AgentResponse(
            explanation="Here is the plan",
            response=HelpResponse(question="Does this look good?"),
        )

        mock_ctx = MagicMock(spec=WorkflowContext)
        mock_ctx.send_message = AsyncMock()
        mock_ctx.request_info = AsyncMock()

        await executor.handle_help(agent_response, mock_ctx)

        mock_ctx.request_info.assert_called_once()
        mock_ctx.send_message.assert_not_called()


class TestPresentationHelpHandlerAutopilot:
    """PresentationHelpHandlerExecutor auto-accepts in autopilot mode."""

    @pytest.mark.asyncio
    async def test_autopilot_yields_output(self, mock_chat_client):
        llm_executor = BaseLLMExecutor(mock_chat_client, "Test", 10)
        plan = Plan()
        plan.add_step("Step 1")
        executor = PresentationHelpHandlerExecutor(
            presentation_llm_executor=llm_executor,
            execution_llm_executor=llm_executor,
            plan=plan,
            autopilot=True,
        )

        agent_response = AgentResponse(
            explanation="Results summary",
            response=HelpResponse(question="Are you satisfied?"),
        )

        mock_ctx = MagicMock(spec=WorkflowContext)
        mock_ctx.yield_output = AsyncMock()
        mock_ctx.request_info = AsyncMock()

        await executor.handle_help(agent_response, mock_ctx)

        mock_ctx.yield_output.assert_called_once()
        mock_ctx.request_info.assert_not_called()
        solution = mock_ctx.yield_output.call_args[0][0]
        assert isinstance(solution, SolutionResponse)
        assert "autopilot" in solution.solution.lower()

    @pytest.mark.asyncio
    async def test_non_autopilot_requests_info(self, mock_chat_client):
        llm_executor = BaseLLMExecutor(mock_chat_client, "Test", 10)
        plan = Plan()
        executor = PresentationHelpHandlerExecutor(
            presentation_llm_executor=llm_executor,
            execution_llm_executor=llm_executor,
            plan=plan,
            autopilot=False,
        )

        agent_response = AgentResponse(
            explanation="Results summary",
            response=HelpResponse(question="Are you satisfied?"),
        )

        mock_ctx = MagicMock(spec=WorkflowContext)
        mock_ctx.yield_output = AsyncMock()
        mock_ctx.request_info = AsyncMock()

        await executor.handle_help(agent_response, mock_ctx)

        mock_ctx.request_info.assert_called_once()
        mock_ctx.yield_output.assert_not_called()


# ── Agent-level autopilot tests ──────────────────────────────────────────


class TestAgentAutopilot:
    """Agent-level autopilot behaviour."""

    @pytest.mark.unit
    def test_autopilot_flag_stored(self, autopilot_agent):
        assert autopilot_agent.autopilot is True

    @pytest.mark.unit
    def test_default_agent_not_autopilot(self, mock_environment_variables):
        with (
            patch("agent_bot.plan_then_execute.agent.AzureOpenAIChatClient") as mock_cls,
            patch.object(_agent_module, "render_planning_prompt", return_value="p"),
        ):
            mock_cls.return_value = MagicMock()
            agent = PlanThenExecuteAgent(llm="gpt-4o")
            assert agent.autopilot is False

    @pytest.mark.asyncio
    async def test_autopilot_auto_resolves_request_info(self, autopilot_agent):
        """Safety-net: request_info events are auto-resolved without input_handler."""
        req = _make_request_event("req-1", "Which dataset?")
        first_result = _make_result(pending=[req])
        solution = SolutionResponse(action="solution", solution="Done")
        second_result = _make_result(outputs=[solution])

        mock_workflow = MagicMock()
        mock_workflow.run = AsyncMock(side_effect=[first_result, second_result])
        autopilot_agent._build_workflow = AsyncMock(return_value=mock_workflow)

        # input_handler should NOT be called in autopilot mode
        handler = AsyncMock(return_value="should not be called")
        msg = await autopilot_agent.go("Analyze grid", input_handler=handler)

        assert msg.text == "Done"
        handler.assert_not_awaited()
        # The auto-resolved response should contain "best judgment"
        auto_response = mock_workflow.run.call_args_list[1][1]["responses"]["req-1"]
        assert "best judgment" in auto_response
