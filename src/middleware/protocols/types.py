"""Core data types for Agora middleware protocols.

These are minimal, framework-agnostic representations of the data
that flows through agent middleware. Each agent framework adapter
is responsible for converting its native types to/from these.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Message:
    """A single message in an agent conversation.

    Attributes
    ----------
    role : str
        One of "system", "user", "assistant", or "tool".
    content : str
        The text content of the message.
    metadata : dict[str, Any]
        Framework-specific metadata (tool call IDs, etc.).
    """

    role: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolCall:
    """A request from the agent to invoke a tool.

    Attributes
    ----------
    id : str
        Unique identifier for this tool call (for correlation with results).
    name : str
        The tool/function name being invoked.
    arguments : dict[str, Any]
        The arguments passed to the tool.
    """

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    """The result of a tool invocation.

    Attributes
    ----------
    call_id : str
        The tool call ID this result corresponds to.
    content : str
        The string result returned by the tool.
    is_error : bool
        Whether the tool call resulted in an error.
    """

    call_id: str
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class FunctionInfo:
    """Metadata about a registered tool/function.

    Attributes
    ----------
    name : str
        The tool name as registered in the agent.
    description : str
        Human-readable description of what the tool does.
    """

    name: str
    description: str = ""
