"""MAF (Microsoft Agent Framework) adapter for Agora middleware protocols.

This module bridges the Agora middleware protocol types to their
MAF equivalents, allowing Agora-protocol middleware to be used
in MAF-based agents.

Usage
-----
Wrapping an Agora middleware for use in a MAF agent::

    from middleware.decision_log.adapters.maf_protocols import wrap_chat_middleware

    agora_mw = MyAgoraChatMiddleware()
    maf_mw = wrap_chat_middleware(agora_mw)
    agent = Agent(..., middleware=[maf_mw])

Adapting a MAF chat client for use with :class:`~middleware.decision_log.DecisionLogChatMiddleware`::

    from middleware.decision_log.adapters.maf_protocols import MAFChatClientAdapter

    chat_client = MAFChatClientAdapter(maf_client)
    agora_mw = DecisionLogChatMiddleware(log, agent_name, chat_client)
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Sequence

try:
    from agent_framework import (
        Agent as MAFAgent,
        ContextProvider as MAFBaseContextProvider,
        ChatContext as MAFChatContext,
        ChatMiddleware as MAFChatMiddleware,
        FunctionInvocationContext as MAFFunctionInvocationContext,
        FunctionMiddleware as MAFFunctionMiddleware,
        Message as MAFMessage,
        MiddlewareTermination as MAFMiddlewareTermination,
    )
except ImportError as e:
    raise ImportError(
        "agent-framework is required for MAF adapters. "
        "Install with: pip install agora-workbench[maf]"
    ) from e

from middleware.protocols import (
    ChatClient,
    ChatMiddleware,
    ContextProvider,
    FunctionMiddleware,
    FunctionInfo,
    Message,
    MiddlewareTermination,
    ToolCall,
    ToolResult,
)


# ---------------------------------------------------------------------------
# Type converters
# ---------------------------------------------------------------------------


def _maf_message_to_agora(msg: MAFMessage) -> Message:
    """Convert a MAF Message to an Agora Message."""
    return Message(
        role=getattr(msg, "role", "assistant"),
        content=getattr(msg, "text", "") or getattr(msg, "content", "") or "",
        metadata=dict(getattr(msg, "additional_properties", None) or {}),
    )


def _agora_message_to_maf(msg: Message) -> MAFMessage:
    """Convert an Agora Message to a MAF Message."""
    return MAFMessage(
        role=msg.role,
        text=msg.content,
        additional_properties=msg.metadata,
    )


def _extract_function_info(maf_func: Any) -> FunctionInfo:
    """Extract FunctionInfo from a MAF function object."""
    return FunctionInfo(
        name=getattr(maf_func, "name", str(maf_func)),
        description=getattr(maf_func, "description", ""),
    )


# ---------------------------------------------------------------------------
# Context adapters
# ---------------------------------------------------------------------------


class MAFChatContextAdapter:
    """Adapts a MAF ChatContext to the Agora ChatContext protocol."""

    def __init__(self, maf_ctx: MAFChatContext) -> None:
        self._ctx = maf_ctx

    @property
    def messages(self) -> Sequence[Message]:
        raw = getattr(self._ctx, "messages", []) or []
        return [_maf_message_to_agora(m) for m in raw]

    @property
    def result(self) -> Message | None:
        raw = getattr(self._ctx, "result", None)
        return _maf_message_to_agora(raw) if raw else None

    @property
    def tool_calls(self) -> Sequence[ToolCall]:
        result = getattr(self._ctx, "result", None)
        if result is None:
            return []
        raw_calls = getattr(result, "tool_calls", []) or []
        return [
            ToolCall(
                id=getattr(tc, "id", ""),
                name=getattr(tc, "name", getattr(tc, "function_name", "")),
                arguments=getattr(tc, "arguments", {}),
            )
            for tc in raw_calls
        ]


class MAFFunctionContextAdapter:
    """Adapts a MAF FunctionInvocationContext to the Agora protocol."""

    def __init__(self, maf_ctx: MAFFunctionInvocationContext) -> None:
        self._ctx = maf_ctx

    @property
    def function(self) -> FunctionInfo:
        return _extract_function_info(self._ctx.function)

    @property
    def arguments(self) -> dict[str, Any]:
        args = self._ctx.arguments
        if hasattr(args, "model_dump"):
            return args.model_dump()
        if isinstance(args, dict):
            return args
        return {}

    @arguments.setter
    def arguments(self, value: dict[str, Any]) -> None:
        """Set arguments, reconstructing the original Pydantic model type if possible."""
        original = self._ctx.arguments
        try:
            self._ctx.arguments = type(original)(**value)
        except Exception:
            self._ctx.arguments = value

    @property
    def result(self) -> ToolResult | None:
        raw = self._ctx.result
        if raw is None:
            return None
        return ToolResult(
            call_id=getattr(raw, "call_id", ""),
            content=str(raw) if not isinstance(raw, str) else raw,
            is_error=False,
        )

    @result.setter
    def result(self, value: ToolResult | None) -> None:
        if value is None:
            self._ctx.result = None
        else:
            self._ctx.result = value.content


class MAFAgentContextAdapter:
    """Adapts a MAF agent's run context to the Agora AgentContext protocol."""

    def __init__(self, maf_context: Any, maf_agent: Any = None) -> None:
        self._ctx = maf_context
        self._agent = maf_agent

    @property
    def tools(self) -> Sequence[FunctionInfo]:
        tools_raw: list[Any] = []
        if self._agent:
            opts = getattr(self._agent, "default_options", {})
            default_tools = opts.get("tools", [])
            mcp_tools = getattr(self._agent, "mcp_tools", [])
            tools_raw = [*default_tools, *mcp_tools]

        tools: list[FunctionInfo] = []
        seen_names: set[str] = set()
        for tool in tools_raw:
            info = _extract_function_info(tool)
            if info.name in seen_names:
                continue
            seen_names.add(info.name)
            tools.append(info)
        return tools

    def extend_messages(self, source_id: str, messages: Sequence[Message]) -> None:
        maf_messages = [_agora_message_to_maf(m) for m in messages]
        if hasattr(self._ctx, "extend_messages"):
            self._ctx.extend_messages(source_id, maf_messages)


# ---------------------------------------------------------------------------
# Middleware wrappers
# ---------------------------------------------------------------------------


class AgoraChatMiddlewareToMAF(MAFChatMiddleware):
    """Wraps an Agora ChatMiddleware so it can run in a MAF agent."""

    def __init__(self, agora_middleware: ChatMiddleware) -> None:
        self._inner = agora_middleware

    async def process(
        self,
        context: MAFChatContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        adapted = MAFChatContextAdapter(context)
        await self._inner.process(adapted, call_next)  # type: ignore[arg-type]


class AgoraFunctionMiddlewareToMAF(MAFFunctionMiddleware):
    """Wraps an Agora FunctionMiddleware so it can run in a MAF agent."""

    def __init__(self, agora_middleware: FunctionMiddleware) -> None:
        self._inner = agora_middleware

    async def process(
        self,
        context: MAFFunctionInvocationContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        adapted = MAFFunctionContextAdapter(context)
        try:
            await self._inner.process(adapted, call_next)  # type: ignore[arg-type]
        except MiddlewareTermination as exc:
            context.result = exc.result
            raise MAFMiddlewareTermination(exc.reason) from exc


class AgoraContextProviderToMAF(MAFBaseContextProvider):
    """Wraps an Agora ContextProvider so it can run as a MAF BaseContextProvider.

    Note: MAF's BaseContextProvider has ``async before_run(agent, session, context, state)``.
    This adapter bridges to the simpler Agora ``async provide(context)`` signature.
    """

    def __init__(self, agora_provider: ContextProvider) -> None:
        source_id = getattr(agora_provider, "source_id", type(agora_provider).__name__)
        super().__init__(source_id=source_id)
        self._inner = agora_provider

    async def before_run(
        self, agent: Any = None, session: Any = None, context: Any = None, state: Any = None, **kwargs: Any
    ) -> None:
        adapted = MAFAgentContextAdapter(context, agent)
        await self._inner.provide(adapted)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Convenience factories
# ---------------------------------------------------------------------------


class MAFChatClientAdapter:
    """Adapts a MAF chat client to the Agora :class:`~middleware.protocols.ChatClient` protocol.

    Use this to supply an LLM client to
    :class:`~middleware.decision_log.DecisionLogChatMiddleware` when running
    inside a MAF agent.

    Args:
        client: The MAF chat client (e.g. an ``openai.AsyncAzureOpenAI`` or
            similar object accepted by ``agent_framework.Agent``).

    Example
    -------
    ```python
    from middleware.decision_log.adapters.maf_protocols import MAFChatClientAdapter

    chat_client = MAFChatClientAdapter(my_maf_openai_client)
    mw = DecisionLogChatMiddleware(log, "my_agent", chat_client)
    ```
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    async def complete(self, messages: Sequence[Message]) -> str:
        """Send *messages* to the MAF agent and return the text response.

        Creates a temporary :class:`agent_framework.Agent` for the synthesis
        call so that the MAF client is used exactly as callers would expect.
        """
        synthesis_agent = MAFAgent(
            client=self._client,
            name="decision_log_synthesiser",
        )
        session = synthesis_agent.create_session()
        maf_messages = [_agora_message_to_maf(m) for m in messages]

        result = await synthesis_agent.run(messages=maf_messages, session=session)
        return getattr(result, "text", None) or ""


def wrap_chat_middleware(mw: ChatMiddleware) -> MAFChatMiddleware:
    """Wrap an Agora ChatMiddleware for use in a MAF agent."""
    return AgoraChatMiddlewareToMAF(mw)


def wrap_function_middleware(mw: FunctionMiddleware) -> MAFFunctionMiddleware:
    """Wrap an Agora FunctionMiddleware for use in a MAF agent."""
    return AgoraFunctionMiddlewareToMAF(mw)


def wrap_context_provider(provider: ContextProvider) -> Any:
    """Wrap an Agora ContextProvider for use as a MAF BaseContextProvider."""
    return AgoraContextProviderToMAF(provider)
