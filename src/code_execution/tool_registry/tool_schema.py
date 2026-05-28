"""
Pydantic models for tool definitions used by code execution servers.

This module provides strongly-typed schemas for tool registry entries,
ensuring consistent structure and validation for tools registered on
MCP code execution servers.
"""

import importlib
import inspect
from typing import Any, FrozenSet, List, Optional, Type

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator


# ============================================================================
# Internal helpers for JSON serialization
# ============================================================================


def _resolve_class(path: str) -> type:
    """Resolve a ``'module.submodule:ClassName'`` or ``'module.submodule.ClassName'`` string to a class."""
    # Allow both 'pkg.mod:Class' and 'pkg.mod.Class'
    if ":" in path:
        module_path, class_name = path.split(":", 1)
    else:
        parts = path.split(".")
        module_path, class_name = ".".join(parts[:-1]), parts[-1]
    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)
    if not inspect.isclass(cls):
        raise TypeError(f"{path!r} did not resolve to a class")
    return cls


def _class_to_string(cls: type) -> str:
    """Return the fully-qualified ``'module.qualname'`` string for *cls*."""
    return f"{cls.__module__}.{cls.__qualname__}"


# ============================================================================
# Models
# ============================================================================


class ToolParameter(BaseModel):
    """Schema for a tool parameter (required or optional)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)  # Allow Type objects in fields

    name: str = Field(..., description="Parameter name")
    type: Type[Any] = Field(..., description="Parameter type (e.g., str, int, float, bool, dict, list)")
    description: str = Field(default="", description="Parameter description")
    default: Optional[Any] = Field(default=None, description="Default value (for optional parameters)")

    @field_validator("type", mode="before")
    def parse_type_input(cls, v: Any) -> Type[Any]:
        if isinstance(v, str):
            return _resolve_class(v)
        return v

    @field_validator("type", mode="after")
    def validate_type(cls, v: Any) -> Type[Any]:
        if not inspect.isclass(v):
            raise TypeError("Field `type` must be a class/type object.")
        return v

    @field_serializer("type")
    def serialize_type(self, v: Type[Any]) -> str:
        return _class_to_string(v)


class ReturnSpec(BaseModel):
    """
    Specification for a single return value from a tool.

    Tool implementation returns a dict mapping names to values:
        return {"builder": builder_obj, "model": model_obj, "summary": {...}}
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str = Field(..., description="Return value name (key in returned dict)")
    type: Type[Any] = Field(..., description="Return value type")
    description: str = Field(default="", description="Description of what this return value contains")

    @field_validator("type", mode="before")
    def parse_type_input(cls, v: Any) -> Type[Any]:
        if isinstance(v, str):
            return _resolve_class(v)
        return v

    @field_validator("type", mode="after")
    def validate_type(cls, v: Any) -> Type[Any]:
        if not inspect.isclass(v):
            raise TypeError("Field `type` must be a class/type object.")
        return v

    @field_serializer("type")
    def serialize_type(self, v: Type[Any]) -> str:
        return _class_to_string(v)

    def __repr__(self) -> str:
        return f"ReturnSpec({self.name}: {_class_to_string(self.type)})"


class StateTransition(BaseModel):
    """Preconditions and postconditions for a tool expressed as state tokens.

    State tokens should reference values from a domain's state vocabulary
    enum (e.g., ``DwsimState``).  The ``requires`` set lists states that
    must hold *before* the tool can run; ``produces`` lists states that
    will hold *after* a successful invocation.
    """

    requires: FrozenSet[str] = Field(
        default_factory=frozenset, description="State tokens that must hold before running this tool"
    )
    produces: FrozenSet[str] = Field(
        default_factory=frozenset, description="State tokens that hold after a successful run"
    )

    @field_serializer("requires", "produces")
    def serialize_frozenset(self, v: FrozenSet[str]) -> list[str]:
        return sorted(v)


class ToolDefinition(BaseModel):
    """Schema for a complete tool definition in the registry."""

    model_config = ConfigDict(arbitrary_types_allowed=True)  # Allow Type objects in fields

    name: str = Field(..., description="Tool function name")
    description: str = Field(..., description="What the tool does")
    required_parameters: list[ToolParameter] = Field(default_factory=list, description="List of required parameters")
    optional_parameters: list[ToolParameter] = Field(
        default_factory=list, description="List of optional parameters with defaults"
    )
    return_spec: List[ReturnSpec] = Field(
        default_factory=list, description="List of return value specifications. Each defines a value the tool returns."
    )
    module: str = Field(..., description="Python module path where tool is implemented")
    server_name: Optional[str] = Field(default=None, description="MCP server this tool belongs to")
    state_transition: StateTransition = Field(
        default_factory=StateTransition, description="State preconditions and postconditions"
    )
    affordances: list[str] = Field(
        default_factory=list, description="Natural-language phrases describing what this tool helps accomplish"
    )

    # assigned and utilized by ToolRegistry
    id: Optional[int] = Field(default=None, description="Tool ID in registry")
