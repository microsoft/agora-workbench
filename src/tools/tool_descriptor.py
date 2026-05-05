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

import inspect
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Type

from pydantic import BaseModel


@dataclass
class ToolDescriptor:
    """Framework-agnostic description of an async tool.

    Attributes:
        name: Unique tool name (used by the agent to invoke the tool).
        description: Human-readable description of what the tool does.
        input_model: The Pydantic model class describing the tool's input.
            This is the single source of truth — ``input_schema`` is derived
            from it automatically when not provided explicitly.
        func: The async callable that implements the tool.  It accepts the
            fields described by *input_model* as keyword arguments and returns
            a plain string (typically JSON-encoded).
        input_schema: JSON Schema (as a plain ``dict``) describing the tool's
            input.  Derived automatically from *input_model* if not provided.
            Frameworks that accept JSON Schema directly can use this field
            as-is; MAF adapters use *input_model* for richer type info.
    """

    name: str
    description: str
    input_model: Type[BaseModel]
    func: Callable[..., Awaitable[str]]
    input_schema: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("ToolDescriptor.name must be a non-empty string")
        if not isinstance(self.input_schema, dict):
            raise TypeError("ToolDescriptor.input_schema must be a dict")
        if not (callable(self.func) and inspect.iscoroutinefunction(self.func)):
            raise TypeError("ToolDescriptor.func must be an async callable")
        # Derive input_schema from input_model if not explicitly provided
        if not self.input_schema:
            self.input_schema = self.input_model.model_json_schema()
