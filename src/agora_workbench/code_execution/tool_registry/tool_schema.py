"""
Pydantic models for tool definitions used by code execution servers.

This module provides strongly-typed schemas for tool registry entries,
ensuring consistent structure and validation for tools registered on
MCP code execution servers.
"""

import importlib
import inspect
from enum import Enum
from typing import Any, FrozenSet, List, Optional, Type

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator


# ============================================================================
# Internal helpers for JSON serialization
# ============================================================================


def _resolve_class(path: str) -> type:
    """Resolve a dotted or colon-separated class path string to a class object.

    Accepts both ``'module.submodule:ClassName'`` (explicit colon separator)
    and ``'module.submodule.ClassName'`` (dot-separated) formats.

    Args:
        path: Dotted or colon-separated path to a class, e.g.
            ``'mypackage.mymodule.MyClass'`` or
            ``'mypackage.mymodule:MyClass'``.

    Returns:
        The resolved class object.

    Raises:
        ImportError: If the module portion of *path* cannot be imported.
        AttributeError: If the class name does not exist on the module.
        TypeError: If the resolved object is not a class.
    """
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
    """Return the fully-qualified ``'module.qualname'`` string for a class.

    Args:
        cls: The class to serialize.

    Returns:
        A string of the form ``'<module>.<qualname>'``, e.g.
        ``'mypackage.mymodule.MyClass'``.
    """
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


class State:
    """A named state in a domain's tool workflow graph.

    States represent meaningful intermediate artifacts that tools produce
    and consume. They form the nodes in the state-transition graph that
    powers workflow planning and skill discovery.

    Can be used directly in ``StateTransition.requires`` and
    ``StateTransition.produces`` alongside plain strings::

        MOLECULE_PARSED = State(
            token="chemistry.molecule_parsed",
            description="A SMILES string has been validated and canonicalized",
            affordances=["validate a SMILES string", "identify a molecule from SMILES"],
        )

        parse_molecule = ToolDefinition(
            ...,
            state_transition=StateTransition(produces=frozenset({MOLECULE_PARSED})),
        )

    Attributes:
        token: Canonical string identifier (e.g. ``"chemistry.molecule_parsed"``).
        description: Human-readable explanation of what this state represents.
        affordances: Search phrases describing what achieving this state enables.
    """

    __slots__ = ("token", "description", "affordances")

    def __init__(self, token: str, description: str = "", affordances: list[str] | None = None):
        object.__setattr__(self, "token", token)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "affordances", affordances or [])

    def __setattr__(self, name, value):
        raise AttributeError("State objects are immutable")

    def __hash__(self) -> int:
        return hash(self.token)

    def __eq__(self, other) -> bool:
        if isinstance(other, State):
            return self.token == other.token
        if isinstance(other, str):
            return self.token == other
        return NotImplemented

    def __repr__(self) -> str:
        return f"State({self.token!r})"

    def __str__(self) -> str:
        return self.token


class StateTransition(BaseModel):
    """Preconditions and postconditions for a tool expressed as state tokens.

    State tokens can be plain strings or :class:`State` objects.  When
    ``State`` objects are used, the token string is extracted for storage
    and serialization.  The ``requires`` set lists states that must hold
    *before* the tool can run; ``produces`` lists states that will hold
    *after* a successful invocation.
    """

    requires: FrozenSet[str] = Field(
        default_factory=frozenset, description="State tokens that must hold before running this tool"
    )
    produces: FrozenSet[str] = Field(
        default_factory=frozenset, description="State tokens that hold after a successful run"
    )

    @field_validator("requires", "produces", mode="before")
    @classmethod
    def normalize_state_tokens(cls, v: Any) -> frozenset[str]:
        """Accept State objects, strings, or Enums and normalize to a frozenset of token strings."""

        def _to_token(item: Any) -> str:
            if isinstance(item, State):
                return item.token
            # Support str-Enums (e.g. class MyState(str, Enum)) — use .value
            if isinstance(item, Enum):
                return str(item.value)
            return str(item)

        if isinstance(v, (frozenset, set, list, tuple)):
            return frozenset(_to_token(item) for item in v)
        return v

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
    module: Optional[str] = Field(
        default=None,
        description=(
            "Resolved Python module path for kernel-side import. The tool proxy "
            "generates `from {module} import {name}` inside the execution kernel. "
            "Typically populated automatically by ToolRegistry from the registry's "
            "`package` setting. Can also be set directly for explicit control or "
            "when constructing ToolDefinitions from catalog data."
        ),
    )
    module_override: Optional[str] = Field(
        default=None,
        exclude=True,
        description=(
            "Full module path override for kernel-side import, taking precedence "
            "over the registry's default `package` resolution. Use when a tool's "
            "implementation does not follow the `{package}.{name}` convention — "
            "e.g. multiple tools in a shared module like `mypackage.tools`."
        ),
    )
    server_name: Optional[str] = Field(default=None, description="MCP server this tool belongs to")
    state_transition: StateTransition = Field(
        default_factory=StateTransition, description="State preconditions and postconditions"
    )
    affordances: list[str] = Field(
        default_factory=list, description="Natural-language phrases describing what this tool helps accomplish"
    )

    # assigned and utilized by ToolRegistry
    id: Optional[int] = Field(default=None, description="Tool ID in registry")
