"""Tests for middleware.decision_log.adapters.maf_protocols -- adapter bridging logic."""

import pytest

pytest.importorskip("agent_framework")

from unittest.mock import AsyncMock, MagicMock, patch

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
    MAFChatClientAdapter,
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
# MAFFunctionContextAdapter -- arguments setter round-trip
# ---------------------------------------------------------------------------


class TestMAFFunctionContextAdapterArgumentsSetter:
    """Test the arguments setter handles both Pydantic model reconstruction and dict fallback."""

    @pytest.mark.unit
    def test_reconstructs_pydantic_model_from_dict(self):
        """When original args are a Pydantic model, setter reconstructs the model type."""
        from pydantic import BaseModel
        from middleware.decision_log.adapters.maf_protocols import MAFFunctionContextAdapter

        class MyInput(BaseModel):
            x: int = 0
            y: str = ""

        maf_ctx = MagicMock()
        maf_ctx.function = MagicMock()
        maf_ctx.function.configure_mock(name="test_fn")
        maf_ctx.arguments = MyInput(x=1, y="original")
        maf_ctx.result = None

        adapter = MAFFunctionContextAdapter(maf_ctx)

        # Write new values via the adapter
        adapter.arguments = {"x": 42, "y": "updated"}

        # Should have reconstructed as MyInput on the MAF context
        assert isinstance(maf_ctx.arguments, MyInput)
        assert maf_ctx.arguments.x == 42
        assert maf_ctx.arguments.y == "updated"

    @pytest.mark.unit
    def test_falls_back_to_dict_when_model_reconstruction_fails(self):
        """When model reconstruction raises, falls back to storing a plain dict."""
        from middleware.decision_log.adapters.maf_protocols import MAFFunctionContextAdapter

        class BrokenModel:
            """A non-Pydantic object whose constructor rejects kwargs."""

            def __init__(self):
                pass

            def model_dump(self):
                return {"old": "value"}

        maf_ctx = MagicMock()
        maf_ctx.function = MagicMock()
        maf_ctx.function.configure_mock(name="test_fn")
        maf_ctx.arguments = BrokenModel()
        maf_ctx.result = None

        adapter = MAFFunctionContextAdapter(maf_ctx)

        # Write a plain dict — reconstruction of BrokenModel(**dict) will fail
        adapter.arguments = {"new_key": "new_value"}

        # Should fall back to storing the dict directly
        assert maf_ctx.arguments == {"new_key": "new_value"}

    @pytest.mark.unit
    def test_round_trip_read_write_read(self):
        """Read → write → read round-trip preserves values."""
        from middleware.decision_log.adapters.maf_protocols import MAFFunctionContextAdapter

        maf_ctx = MagicMock()
        maf_ctx.function = MagicMock()
        maf_ctx.function.configure_mock(name="test_fn")
        maf_ctx.arguments = {"a": 1, "b": 2}
        maf_ctx.result = None

        adapter = MAFFunctionContextAdapter(maf_ctx)
        original = adapter.arguments
        assert original == {"a": 1, "b": 2}

        adapter.arguments = {"a": 10, "b": 20}
        assert adapter.arguments == {"a": 10, "b": 20}


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
        from agent_framework import BaseContextProvider

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


# ---------------------------------------------------------------------------
# MAFChatClientAdapter -- wraps MAF client as ChatClient protocol
# ---------------------------------------------------------------------------


class TestMAFChatClientAdapter:
    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_complete_uses_maf_agent(self):
        """MAFChatClientAdapter.complete creates a MAF Agent and returns its text output."""
        from middleware.protocols import Message

        mock_client = MagicMock()
        adapter = MAFChatClientAdapter(mock_client)

        mock_result = MagicMock()
        mock_result.text = "synthesis result"

        with patch("middleware.decision_log.adapters.maf_protocols.MAFAgent") as MockAgent:
            agent_instance = MockAgent.return_value
            agent_instance.create_session.return_value = MagicMock()
            agent_instance.run = AsyncMock(return_value=mock_result)

            messages = [Message(role="system", content="You are helpful")]
            result = await adapter.complete(messages)

        assert result == "synthesis result"
        MockAgent.assert_called_once_with(client=mock_client, name="decision_log_synthesiser")

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_complete_returns_empty_string_on_no_text(self):
        """Returns empty string when result.text is None."""
        from middleware.protocols import Message

        mock_client = MagicMock()
        adapter = MAFChatClientAdapter(mock_client)

        mock_result = MagicMock()
        mock_result.text = None

        with patch("middleware.decision_log.adapters.maf_protocols.MAFAgent") as MockAgent:
            agent_instance = MockAgent.return_value
            agent_instance.create_session.return_value = MagicMock()
            agent_instance.run = AsyncMock(return_value=mock_result)

            result = await adapter.complete([Message(role="user", content="hi")])

        assert result == ""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_reuses_agent_across_calls(self):
        """Agent is created once and reused across multiple complete() calls."""
        from middleware.protocols import Message

        mock_client = MagicMock()
        adapter = MAFChatClientAdapter(mock_client)

        mock_result = MagicMock()
        mock_result.text = "ok"

        with patch("middleware.decision_log.adapters.maf_protocols.MAFAgent") as MockAgent:
            agent_instance = MockAgent.return_value
            agent_instance.create_session.return_value = MagicMock()
            agent_instance.run = AsyncMock(return_value=mock_result)

            await adapter.complete([Message(role="user", content="first")])
            await adapter.complete([Message(role="user", content="second")])

        # Agent constructed only once, but create_session called twice
        MockAgent.assert_called_once()
        assert agent_instance.create_session.call_count == 2
        assert agent_instance.run.call_count == 2
