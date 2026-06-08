"""
Shared protocol and models for tool search backends.

This module is the single source of truth for the tool-search contract.
Both MCP server code (``code_execution``) and client-side orchestration
can depend on it without creating circular imports.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal, Optional

from pydantic import BaseModel, Field

#: Valid categories for filtering search results.
SearchCategory = Literal["all", "tools", "skills"]

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
    type: str = Field(default="tool", description="Result type: 'tool' or 'skill'")
    to_access: str = Field(
        default="",
        description="Instruction for how to access this result (e.g. the tool call to make)",
    )
    score: Optional[float] = Field(default=None, description="Relevance score from the search backend")
    state_requires: list[str] = Field(default_factory=list, description="State tokens required before this tool runs")
    state_produces: list[str] = Field(default_factory=list, description="State tokens produced after a successful run")


class ToolSearchBackend(ABC):
    """Abstract base class for searching a tool catalog.

    Subclasses must implement :meth:`search`.  Implementations can be
    injected into :func:`~code_execution.server.CodeExecutionServer` via
    the ``tool_search_backend`` constructor parameter.

    Lifecycle:
        1. The backend is constructed (lightweight, no data required).
        2. The server calls :meth:`index` with the tool catalog and skills.
        3. If the backend exposes an ``initialize()`` method, the server calls it.
        4. :meth:`search` is called for each user query.
        5. If the backend exposes a ``close()`` method, the server calls it at shutdown.
    """

    def index(self, tools: list[ToolInfo], skills: list[dict] | None = None, server_name: str = "") -> None:
        """Receive the tool catalog and skill metadata to index.

        Called by the server after construction but before any searches.
        The default implementation is a no-op; subclasses should override
        to ingest the provided data.

        Args:
            tools: Tool metadata from the server's tool registry.
            skills: Optional skill metadata dicts discovered from domains.
            server_name: Name of the hosting MCP server.
        """

    @abstractmethod
    async def search(self, query: str, top: int = 5, category: SearchCategory = "all") -> list[ToolSearchResult]:
        """Search for tools and/or skills matching *query*.

        Args:
            query: Natural-language description or tool name.
            top: Maximum number of results to return.
            category: Filter results by type — ``"all"``, ``"tools"``, or
                ``"skills"``.

        Returns:
            List of :class:`ToolSearchResult` ordered by descending relevance.
        """
        raise NotImplementedError
