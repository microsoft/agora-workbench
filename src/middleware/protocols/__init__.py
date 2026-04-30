"""Agora Middleware Protocols.

Framework-agnostic protocol definitions that allow the middleware
components (tool learning, decision log) to integrate with any
agent framework -- MAF, OpenAI Agents SDK, LangGraph, or custom.

Users implement these protocols (or use a provided adapter) to
connect the middleware to their agent's lifecycle hooks.
"""

from .types import Message, ToolCall, ToolResult, FunctionInfo
from .middleware import (
    ChatMiddleware,
    FunctionMiddleware,
    ContextProvider,
    ChatContext,
    FunctionInvocationContext,
    AgentContext,
    MiddlewareTermination,
)

__all__ = [
    "Message",
    "ToolCall",
    "ToolResult",
    "FunctionInfo",
    "ChatMiddleware",
    "FunctionMiddleware",
    "ContextProvider",
    "ChatContext",
    "FunctionInvocationContext",
    "AgentContext",
    "MiddlewareTermination",
]
