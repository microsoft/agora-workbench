"""Tests for middleware.protocols.adapters_maf -- adapter bridging logic."""

import pytest

pytest.importorskip("agent_framework")

from unittest.mock import AsyncMock, MagicMock

from middleware.protocols import (
    FunctionMiddleware,
    ContextProvider,
    MiddlewareTermination,
    ToolCall,
)
from middleware.decision_log.adapters.maf_protocols import (
    AgoraContextProviderToMAF,
    AgoraFunctionMiddlewareToMAF,
    MAFAgentContextAdapter,
    MAFChatContextAdapter,
    _maf_message_to_agora,
)


# ---------------------------------------------------------------------------
# Type converters
# ---------------------------------------------------------------------------


class TestMafMessageToAgora:
    @pytest.mark.unit
    def test_converts_basic_message(self):
        msg = MagicMock(role="user", text="hello", content="", additional_properties={"key": "val"})
        result = _maf_message_to_agora(msg)
        assert result.role == "user"
        assert result.content == "hello"
        assert result.metadata == {"key": "val"}

    @pytest.mark.unit
    def test_none_additional_properties_becomes_empty_dict(self):
        msg = MagicMock(role="assistant", text="hi", additional_properties=None)
        result = _maf_message_to_agora(msg)
        assert result.metadata == {}

    @pytest.mark.unit
    def test_missing_additional_properties_becomes_empty_dict(self):
        msg = MagicMock(role="assistant", text="hi", spec=["role", "text"])
        result = _maf_message_to_agora(msg)
        assert result.metadata == {}


# ---------------------------------------------------------------------------
# MAFAgentContextAdapter -- tools including mcp_tools
# ---------------------------------------------------------------------------


class TestMAFAgentContextAdapterTools:
    @pytest.mark.unit
    def test_combines_default_and_mcp_tools(self):
        tool_a = MagicMock(name="tool_a", description="A")
        tool_b = MagicMock(name="tool_b", description="B")
        # MagicMock.name is special, override via configure_mock
        tool_a.configure_mock(name="tool_a")
        tool_b.configure_mock(name="tool_b")

        agent = MagicMock()
        agent.default_options = {"tools": [tool_a]}
        agent.mcp_tools = [tool_b]

        adapter = MAFAgentContextAdapter(MagicMock(), agent)
        tools = adapter.tools
        names = [t.name for t in tools]
        assert "tool_a" in names
        assert "tool_b" in names

    @pytest.mark.unit
    def test_deduplicates_by_name(self):
        tool = MagicMock()
        tool.configure_mock(name="shared_tool")
        tool.description = "desc"

        agent = MagicMock()
        agent.default_options = {"tools": [tool]}
        agent.mcp_tools = [tool]

        adapter = MAFAgentContextAdapter(MagicMock(), agent)
        assert len(adapter.tools) == 1

    @pytest.mark.unit
    def test_no_agent_returns_empty(self):
        adapter = MAFAgentContextAdapter(MagicMock(), None)
        assert adapter.tools == []


# ---------------------------------------------------------------------------
# MAFChatContextAdapter -- tool_calls conversion
# ---------------------------------------------------------------------------


class TestMAFChatContextAdapterToolCalls:
    @pytest.mark.unit
    def test_converts_tool_calls_to_typed(self):
        tc = MagicMock(id="call_1", name="run_sim", arguments={"x": 1})
        tc.configure_mock(name="run_sim")
        result_msg = MagicMock(tool_calls=[tc])
        ctx = MagicMock(result=result_msg)

        adapter = MAFChatContextAdapter(ctx)
        calls = adapter.tool_calls
        assert len(calls) == 1
        assert isinstance(calls[0], ToolCall)
        assert calls[0].name == "run_sim"
        assert calls[0].arguments == {"x": 1}

    @pytest.mark.unit
    def test_no_result_returns_empty(self):
        ctx = MagicMock(result=None)
        adapter = MAFChatContextAdapter(ctx)
        assert adapter.tool_calls == []


# ---------------------------------------------------------------------------
# AgoraFunctionMiddlewareToMAF -- MiddlewareTermination propagation
# ---------------------------------------------------------------------------


class TestFunctionMiddlewareTerminationPropagation:
    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_blocked_call_sets_context_result(self):
        """When middleware raises MiddlewareTermination, context.result is set."""
        from agent_framework import MiddlewareTermination as MAFTermination

        class BlockingMiddleware(FunctionMiddleware):
            async def process(self, context, call_next):
                raise MiddlewareTermination(reason="blocked", result="use alternative")

        mw = AgoraFunctionMiddlewareToMAF(BlockingMiddleware())
        maf_ctx = MagicMock()
        maf_ctx.function = MagicMock(name="test_fn")
        maf_ctx.function.configure_mock(name="test_fn")
        maf_ctx.arguments = {}
        maf_ctx.result = None

        with pytest.raises(MAFTermination) as exc_info:
            await mw.process(maf_ctx, AsyncMock())

        # The key assertion: context.result was set before raising
        assert maf_ctx.result == "use alternative"
        assert "blocked" in str(exc_info.value)


# ---------------------------------------------------------------------------
# AgoraContextProviderToMAF -- inherits BaseContextProvider
# ---------------------------------------------------------------------------


class TestContextProviderInheritance:
    @pytest.mark.unit
    def test_inherits_base_context_provider(self):
        from agent_framework import ContextProvider as BaseContextProvider

        class MyProvider(ContextProvider):
            async def provide(self, context):
                pass

        wrapped = AgoraContextProviderToMAF(MyProvider())
        assert isinstance(wrapped, BaseContextProvider)
        assert wrapped.source_id == "MyProvider"

    @pytest.mark.unit
    def test_custom_source_id(self):
        class MyProvider(ContextProvider):
            source_id = "custom_id"

            async def provide(self, context):
                pass

        wrapped = AgoraContextProviderToMAF(MyProvider())
        assert wrapped.source_id == "custom_id"
