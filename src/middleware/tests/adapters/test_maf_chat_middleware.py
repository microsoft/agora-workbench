"""Tests for DecisionLogChatMiddleware (recording & synthesis)."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from middleware.decision_log.adapters.maf_chat_middleware import (
    DECISION_SYNTHESIS_PROMPT,
    DecisionLogChatMiddleware,
    _SynthesisOutput,
    _capture_event,
    _format_buffer,
    _strip_markdown_fence,
)
from middleware.decision_log.entry import DecisionLogEntry
from middleware.decision_log.log import DecisionLog
from middleware.protocols import Message, ToolCall


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _make_chat_context(messages=None, result=None, tool_calls=None):
    """Build a minimal mock Agora ChatContext."""
    ctx = MagicMock()
    ctx.messages = messages or []
    ctx.result = result
    ctx.tool_calls = tool_calls or []
    return ctx


def _make_message(role="user", content="hello"):
    return Message(role=role, content=content)


def _make_tool_call(name="my_tool", call_id="call_1"):
    return ToolCall(id=call_id, name=name, arguments={})


def _make_chat_client() -> MagicMock:
    """Build a mock ChatClient with an async complete() method."""
    client = MagicMock()
    client.complete = AsyncMock(return_value='{"summary": "test", "evidence": {}}')
    return client


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
# _capture_event
# ------------------------------------------------------------------


class TestCaptureEvent:
    @pytest.mark.unit
    def test_captures_input_and_output(self):
        result_msg = _make_message("assistant", "4")
        ctx = _make_chat_context(
            messages=[_make_message("user", "What is 2+2?")],
            result=result_msg,
        )
        event = _capture_event(ctx)
        assert "What is 2+2?" in event["input_summary"]
        assert "4" in event["output_summary"]
        assert event["has_tool_calls"] is False

    @pytest.mark.unit
    def test_captures_tool_call_flag(self):
        ctx = _make_chat_context(
            messages=[_make_message("user", "search")],
            tool_calls=[_make_tool_call()],
        )
        event = _capture_event(ctx)
        assert event["has_tool_calls"] is True

    @pytest.mark.unit
    def test_none_result(self):
        ctx = _make_chat_context(messages=[_make_message("user", "hi")], result=None)
        event = _capture_event(ctx)
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
        mw = DecisionLogChatMiddleware(log, agent_name="coder", chat_client=_make_chat_client(), max_buffer_size=5)
        ctx = _make_chat_context(
            messages=[_make_message("user", "hi")],
            result=_make_message("assistant", ""),
            tool_calls=[_make_tool_call()],
        )
        call_next = AsyncMock()
        await mw.process(ctx, call_next)
        call_next.assert_awaited_once()
        assert len(mw._buffer) == 1

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_synthesis_triggered_no_tool_calls(self):
        """When response has no tool calls, buffer is drained and synthesis queued."""
        log = DecisionLog()
        mw = DecisionLogChatMiddleware(log, agent_name="coder", chat_client=_make_chat_client(), max_buffer_size=5)

        ctx = _make_chat_context(
            messages=[_make_message("user", "do it")],
            result=_make_message("assistant", "Done!"),
            tool_calls=[],
        )
        call_next = AsyncMock()
        await mw.process(ctx, call_next)

        assert len(mw._buffer) == 0
        assert mw._synthesis_queue is not None
        assert mw._synthesis_queue.qsize() >= 1

        await mw.aclose()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_synthesis_triggered_buffer_full(self):
        """When buffer reaches max_buffer_size, synthesis is triggered."""
        log = DecisionLog()
        mw = DecisionLogChatMiddleware(log, agent_name="coder", chat_client=_make_chat_client(), max_buffer_size=2)

        call_next = AsyncMock()

        ctx1 = _make_chat_context(
            messages=[_make_message("user", "step 1")],
            result=_make_message("assistant", ""),
            tool_calls=[_make_tool_call()],
        )
        await mw.process(ctx1, call_next)
        assert len(mw._buffer) == 1

        ctx2 = _make_chat_context(
            messages=[_make_message("user", "step 2")],
            result=_make_message("assistant", ""),
            tool_calls=[_make_tool_call()],
        )
        await mw.process(ctx2, call_next)

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
        mw = DecisionLogChatMiddleware(log, agent_name="coder", chat_client=_make_chat_client(), max_buffer_size=10)

        mw._buffer.append({"input_summary": "q", "output_summary": "a", "has_tool_calls": False})

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
        mw = DecisionLogChatMiddleware(log, agent_name="coder", chat_client=_make_chat_client())
        await mw.flush()
        assert len(log) == 0


# ------------------------------------------------------------------
# DecisionLogChatMiddleware._synthesise_entry
# ------------------------------------------------------------------


class TestSynthesiseEntry:
    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_returns_entry_from_json_response(self):
        """When the synthesis ChatClient returns valid JSON, build entry."""
        log = DecisionLog()
        raw_json = json.dumps({"summary": "Planned next step", "evidence": {"step": "1"}})
        mock_client = MagicMock()
        mock_client.complete = AsyncMock(return_value=raw_json)
        mw = DecisionLogChatMiddleware(log, agent_name="planner", chat_client=mock_client)

        entry = await mw._synthesise_entry("some events", "2026-01-01T00:00:00Z")

        assert entry is not None
        assert entry.summary == "Planned next step"
        assert entry.agent == "planner"
        assert entry.evidence == {"step": "1"}

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_falls_back_to_markdown_fence_stripping(self):
        """Markdown-fenced JSON is parsed correctly."""
        log = DecisionLog()
        raw_json = json.dumps({"summary": "Used search tool", "evidence": {"tool": "search"}})
        mock_client = MagicMock()
        mock_client.complete = AsyncMock(return_value=f"```json\n{raw_json}\n```")
        mw = DecisionLogChatMiddleware(log, agent_name="coder", chat_client=mock_client)

        entry = await mw._synthesise_entry("events", "2026-02-01T00:00:00Z")

        assert entry is not None
        assert entry.summary == "Used search tool"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_returns_none_on_llm_error(self):
        """When the ChatClient.complete raises, return None."""
        log = DecisionLog()
        mock_client = MagicMock()
        mock_client.complete = AsyncMock(side_effect=RuntimeError("LLM down"))
        mw = DecisionLogChatMiddleware(log, agent_name="coder", chat_client=mock_client)

        entry = await mw._synthesise_entry("events", "2026-03-01T00:00:00Z")

        assert entry is None

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_returns_none_on_bad_json(self):
        """When raw text isn't parseable JSON, return None."""
        log = DecisionLog()
        mock_client = MagicMock()
        mock_client.complete = AsyncMock(return_value="this is not valid json")
        mw = DecisionLogChatMiddleware(log, agent_name="coder", chat_client=mock_client)

        entry = await mw._synthesise_entry("events", "2026-04-01T00:00:00Z")

        assert entry is None

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_synthesis_uses_protocol_messages(self):
        """The synthesis call uses Agora Message objects with correct content."""
        log = DecisionLog()
        raw_json = json.dumps({"summary": "test", "evidence": {}})
        mock_client = MagicMock()
        mock_client.complete = AsyncMock(return_value=raw_json)
        mw = DecisionLogChatMiddleware(log, agent_name="coder", chat_client=mock_client)

        await mw._synthesise_entry("some events text", "2026-01-01T00:00:00Z")

        mock_client.complete.assert_awaited_once()
        messages = mock_client.complete.call_args[0][0]
        assert messages[0].role == "system"
        assert messages[0].content == DECISION_SYNTHESIS_PROMPT
        assert "some events text" in messages[1].content


# ------------------------------------------------------------------
# DecisionLogChatMiddleware.aclose
# ------------------------------------------------------------------


class TestAclose:
    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_aclose_noop_when_no_worker(self):
        """aclose() is safe to call when no worker has been started."""
        log = DecisionLog()
        mw = DecisionLogChatMiddleware(log, agent_name="coder", chat_client=_make_chat_client())
        await mw.aclose()
        assert mw._worker_task is None
        assert mw._synthesis_queue is None

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_aclose_cancels_worker(self):
        """aclose() cancels the background worker task."""
        log = DecisionLog()
        mw = DecisionLogChatMiddleware(log, agent_name="coder", chat_client=_make_chat_client())

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
        mw = DecisionLogChatMiddleware(log, agent_name="coder", chat_client=_make_chat_client())

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
        mw = DecisionLogChatMiddleware(log, agent_name="coder", chat_client=_make_chat_client())
        mw._ensure_worker()

        await mw.aclose()
        await mw.aclose()

        assert mw._worker_task is None
        assert mw._synthesis_queue is None
