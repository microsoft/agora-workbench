"""
Framework-agnostic tool descriptor.

A :class:`ToolDescriptor` is a plain dataclass capturing everything a
framework needs to register a tool: its name, a human-readable description,
the JSON Schema for its input, and the async callable that implements it.

Any agent framework that accepts a callable and a JSON Schema can wrap a
:class:`ToolDescriptor` in a single import-free adapter.  The MAF-specific
adapters (``tools/search/adapters/maf.py``, ``planning/adapters/maf.py``, …)
are thin converters from ``ToolDescriptor`` → ``FunctionTool``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable


@dataclass
class ToolDescriptor:
    """Framework-agnostic description of an async tool.

    Attributes:
        name: Unique tool name (used by the agent to invoke the tool).
        description: Human-readable description of what the tool does.
        input_schema: JSON Schema (as a plain ``dict``) describing the tool's
            input.  Frameworks that accept JSON Schema directly can use this
            field as-is.  MAF adapters derive the ``input_model`` from the
            accompanying Pydantic class instead, but the two must stay in sync.
        func: The async callable that implements the tool.  It accepts the
            fields described by *input_schema* as keyword arguments and returns
            a plain string (typically JSON-encoded).
    """

    name: str
    description: str
    input_schema: dict
    func: Callable[..., Awaitable[str]]
