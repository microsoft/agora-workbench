"""Tests for DecisionLogChatMiddleware (recording & synthesis)."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from middleware.decision_log.chat_middleware import (
    DECISION_SYNTHESIS_PROMPT,
    DecisionLogChatMiddleware,
    _SynthesisOutput,
    _capture_event,
    _format_buffer,
    _response_has_tool_calls,
    _strip_markdown_fence,
)
from middleware.decision_log.entry import DecisionLogEntry
from middleware.decision_log.log import DecisionLog


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _make_chat_context(messages=None, result=None):
    """Build a minimal mock ChatContext."""
    ctx = MagicMock()
    ctx.messages = messages or []
    ctx.result = result
    return ctx


def _make_message(role="user", text="hello"):
    msg = MagicMock()
    msg.role = role
    msg.text = text
    msg.items = []
    return msg


def _make_response(messages=None):
    resp = MagicMock()
    resp.messages = messages or []
    return resp


def _make_tool_call_message():
    item = MagicMock()
    item.type = "function_call"
    msg = MagicMock()
    msg.role = "assistant"
    msg.text = ""
    msg.items = [item]
    return msg


# ------------------------------------------------------------------
# _strip_markdown_fence
# ------------------------------------------------------------------


class TestStripMarkdownFence:
    @pytest.mark.unit
    def test_removes_json_fence(self):
        raw = '```json\n{"key": "value"}\n```'
        assert _strip_markdown_fence(raw) == '{"key": "value"}'

    @pytest.mark.unit
    def test_removes_plain_fence(self):
        raw = "```\nhello\n```"
        assert _strip_markdown_fence(raw) == "hello"

    @pytest.mark.unit
    def test_no_fence_unchanged(self):
        raw = '{"key": "value"}'
        assert _strip_markdown_fence(raw) == '{"key": "value"}'

    @pytest.mark.unit
    def test_whitespace_around_fence(self):
        raw = '  ```json\n{"a": 1}\n```  '
        assert _strip_markdown_fence(raw) == '{"a": 1}'


# ------------------------------------------------------------------
# _SynthesisOutput
# ------------------------------------------------------------------


class TestSynthesisOutput:
    @pytest.mark.unit
    def test_valid_output(self):
        data = {
            "summary": "Agent analysed the error",
            "evidence": {"error_code": "404"},
        }
        out = _SynthesisOutput.model_validate(data)
        assert out.summary == "Agent analysed the error"
        assert out.evidence == {"error_code": "404"}

    @pytest.mark.unit
    def test_evidence_defaults_to_empty(self):
        data = {"summary": "Used search"}
        out = _SynthesisOutput.model_validate(data)
        assert out.evidence == {}


# ------------------------------------------------------------------
# _response_has_tool_calls
# ------------------------------------------------------------------


class TestResponseHasToolCalls:
    @pytest.mark.unit
    def test_no_tool_calls(self):
        resp = _make_response([_make_message("assistant", "Hello")])
        assert _response_has_tool_calls(resp) is False

    @pytest.mark.unit
    def test_with_tool_calls(self):
        resp = _make_response([_make_tool_call_message()])
        assert _response_has_tool_calls(resp) is True

    @pytest.mark.unit
    def test_none_response(self):
        assert _response_has_tool_calls(None) is False


# ------------------------------------------------------------------
# _capture_event
# ------------------------------------------------------------------


class TestCaptureEvent:
    @pytest.mark.unit
    def test_captures_input_and_output(self):
        ctx = _make_chat_context(messages=[_make_message("user", "What is 2+2?")])
        resp = _make_response([_make_message("assistant", "4")])
        event = _capture_event(ctx, resp)
        assert "What is 2+2?" in event["input_summary"]
        assert "4" in event["output_summary"]
        assert event["has_tool_calls"] is False

    @pytest.mark.unit
    def test_captures_tool_call_flag(self):
        ctx = _make_chat_context(messages=[_make_message("user", "search")])
        resp = _make_response([_make_tool_call_message()])
        event = _capture_event(ctx, resp)
        assert event["has_tool_calls"] is True

    @pytest.mark.unit
    def test_none_response(self):
        ctx = _make_chat_context(messages=[_make_message("user", "hi")])
        event = _capture_event(ctx, None)
        assert event["output_summary"] == "(no output)"
        assert event["has_tool_calls"] is False


# ------------------------------------------------------------------
# _format_buffer
# ------------------------------------------------------------------


class TestFormatBuffer:
    @pytest.mark.unit
    def test_formats_single_event(self):
        buf = [
            {
                "input_summary": "user asked something",
                "output_summary": "agent replied",
                "has_tool_calls": False,
            }
        ]
        text = _format_buffer(buf)
        assert "Round-trip 1" in text
        assert "user asked something" in text
        assert "agent replied" in text
        assert "[TOOL CALLS]" not in text

    @pytest.mark.unit
    def test_tool_calls_marker(self):
        buf = [
            {
                "input_summary": "search",
                "output_summary": "results",
                "has_tool_calls": True,
            }
        ]
        text = _format_buffer(buf)
        assert "[TOOL CALLS]" in text

    @pytest.mark.unit
    def test_multiple_events(self):
        buf = [
            {"input_summary": "a", "output_summary": "b", "has_tool_calls": False},
            {"input_summary": "c", "output_summary": "d", "has_tool_calls": False},
        ]
        text = _format_buffer(buf)
        assert "Round-trip 1" in text
        assert "Round-trip 2" in text


# ------------------------------------------------------------------
# DecisionLogChatMiddleware.process
# ------------------------------------------------------------------


class TestProcess:
    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_accumulates_events_in_buffer(self):
        """When response has tool calls and buffer isn't full, events accumulate."""
        log = DecisionLog()
        mw = DecisionLogChatMiddleware(log, agent_name="coder", chat_client=MagicMock(), max_buffer_size=5)
        ctx = _make_chat_context(messages=[_make_message("user", "hi")])
        resp = _make_response([_make_tool_call_message()])
        ctx.result = resp

        call_next = AsyncMock()

        # Patch context.result to be set after call_next
        async def set_result():
            ctx.result = resp

        call_next.side_effect = set_result

        await mw.process(ctx, call_next)
        call_next.assert_awaited_once()
        assert len(mw._buffer) == 1

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_synthesis_triggered_no_tool_calls(self):
        """When response has no tool calls, buffer is drained and synthesis queued."""
        log = DecisionLog()
        mw = DecisionLogChatMiddleware(log, agent_name="coder", chat_client=MagicMock(), max_buffer_size=5)

        resp = _make_response([_make_message("assistant", "Done!")])
        ctx = _make_chat_context(messages=[_make_message("user", "do it")])
        ctx.result = resp

        call_next = AsyncMock()

        await mw.process(ctx, call_next)

        # Buffer should be cleared after synthesis trigger
        assert len(mw._buffer) == 0
        # Queue should have an item
        assert mw._synthesis_queue is not None
        assert mw._synthesis_queue.qsize() >= 1

        await mw.aclose()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_synthesis_triggered_buffer_full(self):
        """When buffer reaches max_buffer_size, synthesis is triggered."""
        log = DecisionLog()
        mw = DecisionLogChatMiddleware(log, agent_name="coder", chat_client=MagicMock(), max_buffer_size=2)

        call_next = AsyncMock()

        # First call with tool calls — buffer size 1, no trigger
        resp1 = _make_response([_make_tool_call_message()])
        ctx1 = _make_chat_context(messages=[_make_message("user", "step 1")])
        ctx1.result = resp1
        await mw.process(ctx1, call_next)
        assert len(mw._buffer) == 1

        # Second call with tool calls — buffer size reaches 2, triggers synthesis
        resp2 = _make_response([_make_tool_call_message()])
        ctx2 = _make_chat_context(messages=[_make_message("user", "step 2")])
        ctx2.result = resp2
        await mw.process(ctx2, call_next)

        # Buffer should be cleared
        assert len(mw._buffer) == 0
        assert mw._synthesis_queue is not None

        await mw.aclose()


# ------------------------------------------------------------------
# DecisionLogChatMiddleware.flush
# ------------------------------------------------------------------


class TestFlush:
    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_flush_drains_buffer(self):
        """flush() should submit any buffered events and wait for queue to drain."""
        log = DecisionLog()
        mock_client = MagicMock()
        mw = DecisionLogChatMiddleware(log, agent_name="coder", chat_client=mock_client, max_buffer_size=10)

        # Manually add an event to the buffer
        mw._buffer.append({"input_summary": "q", "output_summary": "a", "has_tool_calls": False})

        # Patch _synthesise_entry to return a valid entry
        entry = DecisionLogEntry(
            timestamp="2026-01-01T00:00:00Z",
            agent="coder",
            summary="Flushed entry",
        )
        with patch.object(mw, "_synthesise_entry", new_callable=AsyncMock, return_value=entry):
            await mw.flush()

        assert len(mw._buffer) == 0
        assert len(log) == 1
        assert log.entries[0].summary == "Flushed entry"

        await mw.aclose()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_flush_noop_when_empty(self):
        """flush() with empty buffer and no queue should not raise."""
        log = DecisionLog()
        mw = DecisionLogChatMiddleware(log, agent_name="coder", chat_client=MagicMock())
        await mw.flush()
        assert len(log) == 0


# ------------------------------------------------------------------
# DecisionLogChatMiddleware._synthesise_entry
# ------------------------------------------------------------------


class TestSynthesiseEntry:
    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_returns_entry_from_structured_output(self):
        """When the synthesis Agent returns a _SynthesisOutput value, build entry."""
        log = DecisionLog()
        mock_client = MagicMock()
        mw = DecisionLogChatMiddleware(log, agent_name="planner", chat_client=mock_client)

        synthesis_output = _SynthesisOutput(
            summary="Planned next step",
            evidence={"step": "1"},
        )

        mock_result = MagicMock()
        mock_result.value = synthesis_output
        mock_result.text = ""

        mock_thread = MagicMock()

        with patch("middleware.decision_log.chat_middleware.Agent") as MockAgent:
            agent_instance = MockAgent.return_value
            agent_instance.create_session.return_value = mock_thread
            agent_instance.run = AsyncMock(return_value=mock_result)

            entry = await mw._synthesise_entry("some events", "2026-01-01T00:00:00Z")

        assert entry is not None
        assert entry.summary == "Planned next step"
        assert entry.agent == "planner"
        assert entry.evidence == {"step": "1"}

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_falls_back_to_json_parsing(self):
        """When value is not _SynthesisOutput, parse raw text as JSON."""
        log = DecisionLog()
        mw = DecisionLogChatMiddleware(log, agent_name="coder", chat_client=MagicMock())

        raw_json = json.dumps(
            {
                "summary": "Used search tool",
                "evidence": {"tool": "search"},
            }
        )

        mock_result = MagicMock()
        mock_result.value = None  # No structured output
        mock_result.text = f"```json\n{raw_json}\n```"

        with patch("middleware.decision_log.chat_middleware.Agent") as MockAgent:
            agent_instance = MockAgent.return_value
            agent_instance.create_session.return_value = MagicMock()
            agent_instance.run = AsyncMock(return_value=mock_result)

            entry = await mw._synthesise_entry("events", "2026-02-01T00:00:00Z")

        assert entry is not None
        assert entry.summary == "Used search tool"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_returns_none_on_llm_error(self):
        """When the LLM call raises, return None."""
        log = DecisionLog()
        mw = DecisionLogChatMiddleware(log, agent_name="coder", chat_client=MagicMock())

        with patch("middleware.decision_log.chat_middleware.Agent") as MockAgent:
            agent_instance = MockAgent.return_value
            agent_instance.create_session.return_value = MagicMock()
            agent_instance.run = AsyncMock(side_effect=RuntimeError("LLM down"))

            entry = await mw._synthesise_entry("events", "2026-03-01T00:00:00Z")

        assert entry is None

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_returns_none_on_bad_json(self):
        """When raw text isn't parseable JSON, return None."""
        log = DecisionLog()
        mw = DecisionLogChatMiddleware(log, agent_name="coder", chat_client=MagicMock())

        mock_result = MagicMock()
        mock_result.value = "not a SynthesisOutput"
        mock_result.text = "this is not valid json"

        with patch("middleware.decision_log.chat_middleware.Agent") as MockAgent:
            agent_instance = MockAgent.return_value
            agent_instance.create_session.return_value = MagicMock()
            agent_instance.run = AsyncMock(return_value=mock_result)

            entry = await mw._synthesise_entry("events", "2026-04-01T00:00:00Z")

        assert entry is None

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_synthesis_uses_configured_client(self):
        """The synthesis agent is created with the configured client (model baked into client)."""
        log = DecisionLog()
        mock_client = MagicMock()
        mw = DecisionLogChatMiddleware(
            log,
            agent_name="coder",
            chat_client=mock_client,
        )

        mock_result = MagicMock()
        mock_result.value = _SynthesisOutput(summary="test")

        with patch("middleware.decision_log.chat_middleware.Agent") as MockAgent:
            agent_instance = MockAgent.return_value
            agent_instance.create_session.return_value = MagicMock()
            agent_instance.run = AsyncMock(return_value=mock_result)

            await mw._synthesise_entry("events", "2026-01-01T00:00:00Z")

            MockAgent.assert_called_once_with(
                client=mock_client,
                name="decision_log_synthesiser",
            )

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_synthesis_prompt_in_messages(self):
        """The synthesis prompt should be sent as a system message."""
        log = DecisionLog()
        mw = DecisionLogChatMiddleware(log, agent_name="coder", chat_client=MagicMock())

        mock_result = MagicMock()
        mock_result.value = _SynthesisOutput(summary="test")

        with patch("middleware.decision_log.chat_middleware.Agent") as MockAgent:
            agent_instance = MockAgent.return_value
            agent_instance.create_session.return_value = MagicMock()
            agent_instance.run = AsyncMock(return_value=mock_result)

            await mw._synthesise_entry("some events text", "2026-01-01T00:00:00Z")

            call_kwargs = agent_instance.run.call_args[1]
            messages = call_kwargs["messages"]
            assert messages[0].role == "system"
            assert messages[0].text == DECISION_SYNTHESIS_PROMPT
            assert "some events text" in messages[1].text


# ------------------------------------------------------------------
# DecisionLogChatMiddleware.aclose
# ------------------------------------------------------------------


class TestAclose:
    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_aclose_noop_when_no_worker(self):
        """aclose() is safe to call when no worker has been started."""
        log = DecisionLog()
        mw = DecisionLogChatMiddleware(log, agent_name="coder", chat_client=MagicMock())
        await mw.aclose()
        assert mw._worker_task is None
        assert mw._synthesis_queue is None

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_aclose_cancels_worker(self):
        """aclose() cancels the background worker task."""
        log = DecisionLog()
        mw = DecisionLogChatMiddleware(log, agent_name="coder", chat_client=MagicMock())

        # Force worker creation
        mw._ensure_worker()
        assert mw._worker_task is not None
        assert not mw._worker_task.done()

        await mw.aclose()

        assert mw._worker_task is None
        assert mw._synthesis_queue is None

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_aclose_flushes_buffer(self):
        """aclose() flushes buffered events before shutting down."""
        log = DecisionLog()
        mock_client = MagicMock()
        mw = DecisionLogChatMiddleware(log, agent_name="coder", chat_client=mock_client)

        mw._buffer.append({"input_summary": "q", "output_summary": "a", "has_tool_calls": False})

        entry = DecisionLogEntry(
            timestamp="2026-01-01T00:00:00Z",
            agent="coder",
            summary="Closed entry",
        )
        with patch.object(mw, "_synthesise_entry", new_callable=AsyncMock, return_value=entry):
            await mw.aclose()

        assert len(mw._buffer) == 0
        assert len(log) == 1
        assert log.entries[0].summary == "Closed entry"
        assert mw._worker_task is None

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_aclose_idempotent(self):
        """Calling aclose() multiple times is safe."""
        log = DecisionLog()
        mw = DecisionLogChatMiddleware(log, agent_name="coder", chat_client=MagicMock())
        mw._ensure_worker()

        await mw.aclose()
        await mw.aclose()

        assert mw._worker_task is None
        assert mw._synthesis_queue is None
