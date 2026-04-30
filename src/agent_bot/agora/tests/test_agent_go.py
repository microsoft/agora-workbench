"""Unit tests for AgoraAgent.go() request-info/resume loop.

Covers:
- Single output on first run (no request-info events)
- Single pending request → resume → output
- Multiple pending requests in one round → resume → output
- No outputs and no pending requests (fallback message)
- SolutionResponse, HelpResponse, and generic output formatting
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from .. import agent as _agent_module
from ..agent import AgoraAgent
from ..response_models import SolutionResponse, HelpResponse


def _make_result(outputs=None, pending=None):
    """Create a mock WorkflowRunResult with configurable outputs and pending events."""
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
def agent(mock_environment_variables):
    """Create an AgoraAgent with mocked dependencies."""
    with (
        patch("agent_bot.agora.agent.AzureOpenAIChatClient") as mock_client_class,
        patch.object(_agent_module, "render_system_prompt", return_value="System prompt"),
    ):
        mock_client_class.return_value = MagicMock()
        yield AgoraAgent(llm="gpt-4o")


class TestGoRequestInfoLoop:
    """Tests for the interactive request-info/resume loop in AgoraAgent.go()."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_immediate_output_no_requests(self, agent):
        """When the first run produces outputs immediately, return without looping."""
        solution = SolutionResponse(action="solution", solution="42")
        result = _make_result(outputs=[solution])

        mock_workflow = MagicMock()
        mock_workflow.run = AsyncMock(return_value=result)
        agent._build_workflow = AsyncMock(return_value=mock_workflow)

        msg = await agent.go("What is 6*7?")

        assert msg.role == "assistant"
        assert msg.text == "42"
        # workflow.run called once with the prompt, never resumed
        mock_workflow.run.assert_awaited_once_with("What is 6*7?")

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_single_request_info_then_output(self, agent):
        """One pending request → collect input → resume → output."""
        req_event = _make_request_event("req-1", "What region?")
        first_result = _make_result(pending=[req_event])
        solution = SolutionResponse(action="solution", solution="Texas grid solved")
        second_result = _make_result(outputs=[solution])

        mock_workflow = MagicMock()
        mock_workflow.run = AsyncMock(side_effect=[first_result, second_result])
        agent._build_workflow = AsyncMock(return_value=mock_workflow)

        handler = AsyncMock(return_value="Texas")
        msg = await agent.go("Solve the grid", input_handler=handler)

        assert msg.text == "Texas grid solved"
        # Handler called with question and context
        handler.assert_awaited_once_with("What region?", "")
        # Second run called with responses dict
        mock_workflow.run.assert_awaited_with(responses={"req-1": "Texas"})
        assert mock_workflow.run.await_count == 2

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_multiple_pending_requests(self, agent):
        """Multiple pending requests in one round are all collected before resuming."""
        req1 = _make_request_event("req-a", "Which region?", context="US regions")
        req2 = _make_request_event("req-b", "Which year?")
        first_result = _make_result(pending=[req1, req2])
        solution = SolutionResponse(action="solution", solution="Done")
        second_result = _make_result(outputs=[solution])

        mock_workflow = MagicMock()
        mock_workflow.run = AsyncMock(side_effect=[first_result, second_result])
        agent._build_workflow = AsyncMock(return_value=mock_workflow)

        responses = iter(["California", "2025"])
        handler = AsyncMock(side_effect=lambda q, c="": next(responses))
        msg = await agent.go("Analyze", input_handler=handler)

        assert msg.text == "Done"
        assert handler.await_count == 2
        # Verify both responses sent together
        mock_workflow.run.assert_awaited_with(responses={"req-a": "California", "req-b": "2025"})

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_multiple_rounds_of_requests(self, agent):
        """Two consecutive rounds of request-info before final output."""
        req1 = _make_request_event("r1", "What model?")
        round1 = _make_result(pending=[req1])

        req2 = _make_request_event("r2", "What solver?")
        round2 = _make_result(pending=[req2])

        solution = SolutionResponse(action="solution", solution="Optimized")
        final = _make_result(outputs=[solution])

        mock_workflow = MagicMock()
        mock_workflow.run = AsyncMock(side_effect=[round1, round2, final])
        agent._build_workflow = AsyncMock(return_value=mock_workflow)

        answers = iter(["gpt-4o", "highs"])
        handler = AsyncMock(side_effect=lambda q, c="": next(answers))
        msg = await agent.go("Run optimization", input_handler=handler)

        assert msg.text == "Optimized"
        assert mock_workflow.run.await_count == 3
        assert handler.await_count == 2

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_no_outputs_no_pending_fallback(self, agent):
        """When there are no outputs and no pending requests, return fallback message."""
        result = _make_result(outputs=[], pending=[])

        mock_workflow = MagicMock()
        mock_workflow.run = AsyncMock(return_value=result)
        agent._build_workflow = AsyncMock(return_value=mock_workflow)

        msg = await agent.go("Hello")

        assert msg.role == "assistant"
        assert msg.text == "Workflow completed without final output"
        mock_workflow.run.assert_awaited_once_with("Hello")

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_help_response_output(self, agent):
        """HelpResponse output returns the question text."""
        help_resp = HelpResponse(action="help", question="Could you clarify?")
        result = _make_result(outputs=[help_resp])

        mock_workflow = MagicMock()
        mock_workflow.run = AsyncMock(return_value=result)
        agent._build_workflow = AsyncMock(return_value=mock_workflow)

        msg = await agent.go("Do something ambiguous")

        assert msg.text == "Could you clarify?"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_generic_output_uses_str(self, agent):
        """Non-model outputs fall through to str() conversion."""
        result = _make_result(outputs=["raw string output"])

        mock_workflow = MagicMock()
        mock_workflow.run = AsyncMock(return_value=result)
        agent._build_workflow = AsyncMock(return_value=mock_workflow)

        msg = await agent.go("test")

        assert msg.text == "raw string output"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_multiple_outputs_uses_last(self, agent):
        """When multiple outputs exist, the last one is used."""
        first = SolutionResponse(action="solution", solution="partial")
        last = SolutionResponse(action="solution", solution="final answer")
        result = _make_result(outputs=[first, last])

        mock_workflow = MagicMock()
        mock_workflow.run = AsyncMock(return_value=result)
        agent._build_workflow = AsyncMock(return_value=mock_workflow)

        msg = await agent.go("test")

        assert msg.text == "final answer"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_request_event_without_question_attr(self, agent):
        """When request data lacks a 'question' attr, falls back to str(data)."""
        event = MagicMock()
        event.request_id = "req-x"
        event.data = "plain string data"  # no .question attribute

        first_result = _make_result(pending=[event])
        solution = SolutionResponse(action="solution", solution="ok")
        second_result = _make_result(outputs=[solution])

        mock_workflow = MagicMock()
        mock_workflow.run = AsyncMock(side_effect=[first_result, second_result])
        agent._build_workflow = AsyncMock(return_value=mock_workflow)

        handler = AsyncMock(return_value="user reply")
        msg = await agent.go("test", input_handler=handler)

        # Should have called handler with str(data) as question
        handler.assert_awaited_once_with("plain string data", "")
        assert msg.text == "ok"
