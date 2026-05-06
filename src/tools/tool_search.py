"""
Shared protocol and models for tool search backends.

Defines the contract that ``tools/`` implements — ``core`` owns the
interface so there is no reverse dependency from ``core`` → ``tools``.
"""

from abc import ABC, abstractmethod
from typing import Optional

from pydantic import BaseModel, Field

#: ``(server_name, tool_name)`` – uniquely identifies a tool across MCP servers.
ToolKey = tuple[str, str]


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

    Subclasses must implement :meth:`search`.  Authentication is handled
    by the credential factories in ``auth.providers`` — backends obtain
    credentials directly rather than accepting user tokens.
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
        ...
