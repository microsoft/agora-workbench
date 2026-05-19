"""
Shared protocol and models for tool search backends.

This module is the single source of truth for the tool-search contract.
Both MCP server code (``code_execution``) and client-side orchestration
can depend on it without creating circular imports.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from pydantic import BaseModel, Field

#: ``(server_name, tool_name)`` – uniquely identifies a tool across MCP servers.
ToolKey = tuple[str, str]


@dataclass(frozen=True)
class ToolInfo:
    """Lightweight tool metadata for search indexing.

    Attributes:
        name: Tool name as exposed by the MCP server
        description: Human-readable description of the tool
        server_name: Name of the MCP server hosting this tool
        affordances: Natural-language phrases that improve search recall
        state_requires: State tokens that must hold before this tool can run
        state_produces: State tokens that hold after a successful run
    """

    name: str
    description: str
    server_name: str
    affordances: tuple[str, ...] = ()
    state_requires: tuple[str, ...] = ()
    state_produces: tuple[str, ...] = ()


class ToolSearchResult(BaseModel):
    """A single search hit returned by a :class:`ToolSearchBackend`."""

    name: str = Field(..., description="Tool name")
    server_name: str = Field(..., description="MCP server name (empty string for local tools)")
    description: str = Field(..., description="Tool description")
    execution_type: str = Field(..., description="Execution type (e.g. 'mcp', 'foundry')")
    score: Optional[float] = Field(default=None, description="Relevance score from the search backend")
    state_requires: list[str] = Field(default_factory=list, description="State tokens required before this tool runs")
    state_produces: list[str] = Field(default_factory=list, description="State tokens produced after a successful run")


class ToolSearchBackend(ABC):
    """Abstract base class for searching a tool catalog.

    Subclasses must implement :meth:`search`.  Implementations can be
    injected into :func:`~code_execution.server.CodeExecutionServer` via
    ``register_search_tool()`` to expose server-side search as an MCP tool.
    """

    @abstractmethod
    async def search(self, query: str, top: int = 5) -> list[ToolSearchResult]:
        """Search for tools matching *query*.

        Args:
            query: Natural-language description or tool name.
            top: Maximum number of results to return.

        Returns:
            List of :class:`ToolSearchResult` ordered by descending relevance.
        """
        raise NotImplementedError
