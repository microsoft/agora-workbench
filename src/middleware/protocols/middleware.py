"""Middleware protocol definitions.

These protocols define the hooks that agent frameworks must implement
(or adapt to) for Agora middleware to function. The design mirrors
common middleware patterns (pre/post processing with call_next chains)
while remaining framework-agnostic.

Implement these directly or use one of the provided adapters
(e.g., ``middleware.protocols.adapters_maf``).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable, Protocol, Sequence, runtime_checkable

from .types import FunctionInfo, Message, ToolCall, ToolResult


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class MiddlewareTermination(Exception):
    """Raised by function middleware to block a tool call from executing.

    When a middleware raises this, the framework should NOT invoke the
    underlying tool. Instead, the provided ``result`` should be returned
    to the agent as the tool's output.

    Parameters
    ----------
    reason : str
        Human-readable explanation of why the call was blocked.
    result : str
        The result string to return to the agent in place of actual execution.
    """

    def __init__(self, reason: str, result: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.result = result or reason


# ---------------------------------------------------------------------------
# Context Protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class ChatContext(Protocol):
    """Context available during a chat-level middleware hook.

    Provides access to the conversation messages and the LLM response
    after the call completes.
    """

    @property
    def messages(self) -> Sequence[Message]:
        """The conversation messages being sent to the LLM."""
        ...

    @property
    def result(self) -> Message | None:
        """The LLM's response message (available after call_next)."""
        ...

    @property
    def tool_calls(self) -> Sequence[ToolCall]:
        """Tool calls in the LLM response, if any."""
        ...


@runtime_checkable
class FunctionInvocationContext(Protocol):
    """Context available during a function/tool-level middleware hook.

    Provides access to the function being called, its arguments,
    and (after execution) its result.
    """

    @property
    def function(self) -> FunctionInfo:
        """Metadata about the tool being invoked."""
        ...

    @property
    def arguments(self) -> dict[str, Any]:
        """The arguments being passed to the tool."""
        ...

    @arguments.setter
    def arguments(self, value: dict[str, Any]) -> None:
        """Override the arguments (e.g., for repair loops)."""
        ...

    @property
    def result(self) -> ToolResult | None:
        """The tool's result (available after call_next)."""
        ...

    @result.setter
    def result(self, value: ToolResult | None) -> None:
        """Set/override the tool result (e.g., for repair loops)."""
        ...


@runtime_checkable
class AgentContext(Protocol):
    """Context available during an agent-level middleware hook (pre-run).

    Provides access to the agent's registered tools for introspection.
    """

    @property
    def tools(self) -> Sequence[FunctionInfo]:
        """The tools currently registered on the agent."""
        ...

    def extend_messages(self, source_id: str, messages: Sequence[Message]) -> None:
        """Inject additional messages into the agent's context.

        Parameters
        ----------
        source_id : str
            Identifier for the middleware injecting messages (for debugging).
        messages : Sequence[Message]
            Messages to prepend/append to the agent context.
        """
        ...


# ---------------------------------------------------------------------------
# Middleware ABCs
# ---------------------------------------------------------------------------


class ChatMiddleware(ABC):
    """Middleware that wraps LLM inference calls.

    Invoked once per LLM round-trip. Use for observing/modifying
    the conversation before/after the LLM responds.

    Example
    -------
    ```python
    class LoggingMiddleware(ChatMiddleware):
        async def process(self, context, call_next):
            print(f"Sending {len(context.messages)} messages")
            await call_next()
            print(f"Got response: {context.result.content[:50]}")
    ```
    """

    @abstractmethod
    async def process(
        self,
        context: ChatContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        """Process the chat context, calling call_next() to proceed.

        Parameters
        ----------
        context : ChatContext
            The current conversation context.
        call_next : Callable
            Invoke to continue the middleware chain / call the LLM.
            Must be awaited exactly once.
        """
        ...


class FunctionMiddleware(ABC):
    """Middleware that wraps individual tool/function invocations.

    Invoked once per tool call. Use for validation, guardrails,
    repair loops, or observation.

    Example
    -------
    ```python
    class TimingMiddleware(FunctionMiddleware):
        async def process(self, context, call_next):
            start = time.time()
            await call_next()
            elapsed = time.time() - start
            print(f"{context.function.name} took {elapsed:.2f}s")
    ```
    """

    @abstractmethod
    async def process(
        self,
        context: FunctionInvocationContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        """Process the function invocation, calling call_next() to proceed.

        Parameters
        ----------
        context : FunctionInvocationContext
            The current tool invocation context.
        call_next : Callable
            Invoke to continue the chain / execute the tool.
            Must be awaited exactly once (unless raising MiddlewareTermination).

        Raises
        ------
        MiddlewareTermination
            To block tool execution and return an alternative result.
        """
        ...


@runtime_checkable
class ChatClient(Protocol):
    """Protocol for making LLM completion calls.

    Implementations wrap specific LLM clients (MAF, OpenAI Agents SDK, etc.)
    and provide a uniform interface for completion requests so that
    middleware (e.g. :class:`~middleware.decision_log.DecisionLogChatMiddleware`)
    can call the LLM without depending on a specific framework.

    Example
    -------
    ```python
    class MyClient:
        async def complete(self, messages):
            response = await openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": m.role, "content": m.content} for m in messages],
            )
            return response.choices[0].message.content
    ```
    """

    async def complete(self, messages: Sequence[Message]) -> str:
        """Send messages to the LLM and return the text response.

        Parameters
        ----------
        messages : Sequence[Message]
            The messages to send (system prompt, user turns, etc.).

        Returns
        -------
        str
            The LLM's text response.
        """
        ...


class ContextProvider(ABC):
    """Provides additional context to the agent before each run.

    Use for injecting dynamic state (decision logs, user preferences,
    skill instructions) into the agent's context window.

    Example
    -------
    ```python
    class TimeProvider(ContextProvider):
        async def provide(self, context):
            context.extend_messages("time", [Message(role="system", content=f"Current time: {now()}")])
    ```
    """

    @abstractmethod
    async def provide(self, context: AgentContext) -> None:
        """Inject context before the agent runs.

        Parameters
        ----------
        context : AgentContext
            The agent context to extend with additional messages.
        """
        ...
