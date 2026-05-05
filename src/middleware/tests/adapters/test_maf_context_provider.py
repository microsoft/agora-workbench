"""Tests for DecisionLogContextProvider (context injection only)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from middleware.decision_log.adapters.maf_context_provider import DecisionLogContextProvider
from middleware.decision_log.entry import DecisionLogEntry
from middleware.decision_log.log import DecisionLog
from middleware.protocols import Message


def _make_agent_context() -> MagicMock:
    """Build a minimal mock Agora AgentContext."""
    ctx = MagicMock()
    ctx.tools = []
    ctx.extend_messages = MagicMock()
    return ctx


class TestConstruction:
    @pytest.mark.unit
    def test_source_id(self):
        log = DecisionLog()
        provider = DecisionLogContextProvider(log)
        assert provider.source_id == "decision_log"

    @pytest.mark.unit
    def test_inject_context_defaults_to_true(self):
        log = DecisionLog()
        provider = DecisionLogContextProvider(log)
        assert provider._inject_context is True

    @pytest.mark.unit
    def test_custom_max_entries(self):
        log = DecisionLog()
        provider = DecisionLogContextProvider(log, max_context_entries=5)
        assert provider._max_context_entries == 5

    @pytest.mark.unit
    def test_chat_middleware_stored(self):
        log = DecisionLog()
        mw = MagicMock()
        provider = DecisionLogContextProvider(log, chat_middleware=mw)
        assert provider._chat_middleware is mw


class TestProvide:
    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_injects_context_when_enabled(self):
        log = DecisionLog()
        log._append(
            DecisionLogEntry(
                timestamp="2026-03-19T18:12:04Z",
                agent="planner",
                summary="Thinking hard",
            )
        )
        provider = DecisionLogContextProvider(log, inject_context=True)
        context = _make_agent_context()
        await provider.provide(context)
        context.extend_messages.assert_called_once()
        source_id, messages = context.extend_messages.call_args[0]
        assert source_id == "decision_log"
        assert len(messages) == 1
        assert isinstance(messages[0], Message)
        assert "decision_log" in messages[0].content
        assert "Thinking hard" in messages[0].content

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_skips_injection_when_disabled(self):
        log = DecisionLog()
        provider = DecisionLogContextProvider(log, inject_context=False)
        context = _make_agent_context()
        await provider.provide(context)
        context.extend_messages.assert_not_called()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_respects_max_context_entries(self):
        log = DecisionLog()
        for i in range(10):
            log._append(
                DecisionLogEntry(
                    timestamp=f"2026-03-{i + 1:02d}T00:00:00Z",
                    agent="planner",
                    summary=f"Decision {i}",
                )
            )
        provider = DecisionLogContextProvider(log, inject_context=True, max_context_entries=3)
        context = _make_agent_context()
        await provider.provide(context)
        _, messages = context.extend_messages.call_args[0]
        injected_text = messages[0].content
        assert "Decision 9" in injected_text
        assert "Decision 8" in injected_text
        assert "Decision 7" in injected_text
        assert "Decision 0" not in injected_text

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_flushes_middleware_before_injection(self):
        log = DecisionLog()
        mock_mw = MagicMock()
        mock_mw.flush = AsyncMock()
        provider = DecisionLogContextProvider(log, inject_context=True, chat_middleware=mock_mw)
        context = _make_agent_context()
        await provider.provide(context)
        mock_mw.flush.assert_awaited_once()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_no_flush_when_no_middleware(self):
        """provide() works fine without a chat_middleware reference."""
        log = DecisionLog()
        provider = DecisionLogContextProvider(log, inject_context=True)
        context = _make_agent_context()
        await provider.provide(context)
        context.extend_messages.assert_called_once()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_message_role_is_user(self):
        """The injected message should have role 'user'."""
        log = DecisionLog()
        log._append(DecisionLogEntry(timestamp="2026-01-01T00:00:00Z", agent="a", summary="Did something"))
        provider = DecisionLogContextProvider(log)
        context = _make_agent_context()
        await provider.provide(context)
        _, messages = context.extend_messages.call_args[0]
        assert messages[0].role == "user"
