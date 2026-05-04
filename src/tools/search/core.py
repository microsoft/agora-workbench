"""
Framework-agnostic search tool descriptor factory.

Provides :func:`create_search_tools_descriptor` — a factory that wraps a
:class:`~tools.tool_search.ToolSearchBackend` in a
:class:`~tools.tool_descriptor.ToolDescriptor`.  No agent-framework imports.

MAF users should import
:func:`tools.search.adapters.maf_core.create_search_tools_function` which
calls this factory and wraps the result in a ``FunctionTool``.
"""

from __future__ import annotations

import json
import logging

from pydantic import BaseModel, Field

from tools.tool_descriptor import ToolDescriptor
from tools.tool_search import ToolSearchBackend

LOGGER = logging.getLogger(__name__)


# ============================================================================
# Input model (framework-agnostic)
# ============================================================================


class SearchToolsInput(BaseModel):
    """Input model for the ``search_tools`` tool."""

    query: str = Field(
        description=(
            "Search query to find domain tools.  Can be a tool name "
            "(e.g. 'run_opf') or a natural language description "
            "(e.g. 'optimal power flow')."
        )
    )
    top: int = Field(
        default=5,
        description="Maximum number of results to return.",
    )


# ============================================================================
# Framework-agnostic factory
# ============================================================================


def create_search_tools_descriptor(backend: ToolSearchBackend) -> ToolDescriptor:
    """Create a ``search_tools`` :class:`~tools.tool_descriptor.ToolDescriptor`.

    The returned descriptor performs a catalog search and returns a JSON object
    with a ``results`` key (list of :class:`~tools.tool_search.ToolSearchResult`
    dicts) and an optional ``error`` key.  It is completely backend-agnostic —
    any object satisfying the :class:`ToolSearchBackend` protocol can be
    injected.

    Args:
        backend: A search backend implementing
                 :class:`~tools.tool_search.ToolSearchBackend`.

    Returns:
        :class:`~tools.tool_descriptor.ToolDescriptor` named ``search_tools``.
    """

    async def search_tools(query: str, top: int = 5) -> str:
        """Search the tool catalog and return matching tools as JSON.

        Args:
            query: Natural-language description or tool name.
            top: Maximum number of results.

        Returns:
            JSON object with ``results`` (list of dicts) and, on failure,
            an ``error`` string.
        """
        LOGGER.info("search_tools called with query: '%s', top=%d", query, top)
        try:
            results = await backend.search(query, top)
            return json.dumps({"results": [r.model_dump() for r in results]})
        except Exception as exc:
            LOGGER.error("search_tools failed for query '%s': %s", query, exc, exc_info=True)
            return json.dumps({"results": [], "error": f"{type(exc).__name__}: {exc}"})

    return ToolDescriptor(
        name="search_tools",
        description=(
            "Search the tool catalog for domain-specific tools by name or description.  "
            "Returns a JSON object with a 'results' array of matching tools (each with "
            "server_name, name, description, execution_type, and relevance score).  "
            "Domain tools are invoked programmatically inside execute_code — use search_tools "
            "to discover their names, signatures, and which server they belong to."
        ),
        input_model=SearchToolsInput,
        func=search_tools,
    )
