"""Unit tests for PlanThenExecuteAgent.go() request-info/resume loop.

Mirrors the test structure from agora.tests.test_agent_go, adapted
for the plan-then-execute agent.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from .. import agent as _agent_module
from ..agent import PlanThenExecuteAgent
from ..response_models import SolutionResponse, HelpResponse


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


@pytest.fixture
def agent(mock_environment_variables):
    """Create a PlanThenExecuteAgent with mocked dependencies."""
    with (
        patch("plan_then_execute.agent.AzureOpenAIChatClient") as mock_client_class,
        patch.object(_agent_module, "render_planning_prompt", return_value="Planning prompt"),
        patch.object(_agent_module, "render_execution_prompt", return_value="Execution prompt"),
        patch.object(_agent_module, "render_presentation_prompt", return_value="Presentation prompt"),
    ):
        mock_client_class.return_value = MagicMock()
        yield PlanThenExecuteAgent(llm="gpt-4o")


class TestGoRequestInfoLoop:
    """Tests for the interactive request-info/resume loop."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_immediate_output(self, agent):
        solution = SolutionResponse(action="solution", solution="42")
        result = _make_result(outputs=[solution])

        mock_workflow = MagicMock()
        mock_workflow.run = AsyncMock(return_value=result)
        agent._build_workflow = AsyncMock(return_value=mock_workflow)

        msg = await agent.go("What is 6*7?")
        assert msg.text == "42"
        mock_workflow.run.assert_awaited_once_with("What is 6*7?")

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_help_then_output(self, agent):
        req = _make_request_event("req-1", "Which dataset?")
        first_result = _make_result(pending=[req])
        solution = SolutionResponse(action="solution", solution="Done")
        second_result = _make_result(outputs=[solution])

        mock_workflow = MagicMock()
        mock_workflow.run = AsyncMock(side_effect=[first_result, second_result])
        agent._build_workflow = AsyncMock(return_value=mock_workflow)

        handler = AsyncMock(return_value="IEEE 14 bus")
        msg = await agent.go("Analyze grid", input_handler=handler)

        assert msg.text == "Done"
        handler.assert_awaited_once_with("Which dataset?", "")
        mock_workflow.run.assert_awaited_with(responses={"req-1": "IEEE 14 bus"})

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_no_outputs_no_pending(self, agent):
        result = _make_result(outputs=[], pending=[])
        mock_workflow = MagicMock()
        mock_workflow.run = AsyncMock(return_value=result)
        agent._build_workflow = AsyncMock(return_value=mock_workflow)

        msg = await agent.go("Hello")
        assert msg.text == "Workflow completed without final output"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_help_response_output(self, agent):
        help_resp = HelpResponse(action="help", question="Need clarification")
        result = _make_result(outputs=[help_resp])
        mock_workflow = MagicMock()
        mock_workflow.run = AsyncMock(return_value=result)
        agent._build_workflow = AsyncMock(return_value=mock_workflow)

        msg = await agent.go("Do something")
        assert msg.text == "Need clarification"


class TestAgentHasPlan:
    """Tests that the agent correctly initializes with a Plan."""

    @pytest.mark.unit
    def test_agent_has_plan(self, agent):
        assert agent.plan is not None
        assert agent.plan.finalized is False
        assert agent.plan.steps == []

    @pytest.mark.unit
    def test_agent_plan_is_manipulable(self, agent):
        agent.plan.add_step("Test step")
        assert len(agent.plan.steps) == 1
        agent.plan.finalize()
        assert agent.plan.finalized is True
