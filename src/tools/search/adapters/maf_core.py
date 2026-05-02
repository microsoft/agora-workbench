"""
Generic tool search catalog.

Provides a ``FunctionTool`` factory:

* :func:`create_search_tools_function` — backend-agnostic catalog search
  (returns JSON object with ``results`` list of
  :class:`~tools.tool_search.ToolSearchResult` dicts and optional ``error``).

Neither factory depends on a specific search backend; callers inject the
backend via the :class:`~tools.tool_search.ToolSearchBackend` protocol.
"""

import json
import logging

try:
    from agent_framework import FunctionTool
except ImportError as e:
    raise ImportError(
        "agent-framework is required for MAF adapters. "
        "Install with: pip install agora-workbench[maf]"
    ) from e
from pydantic import BaseModel, Field

from tools.tool_search import ToolSearchBackend

LOGGER = logging.getLogger(__name__)


# ============================================================================
# Input models
# ============================================================================


class SearchToolsInput(BaseModel):
    """Input model for the ``search_tools`` FunctionTool."""

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
# search_tools factory
# ============================================================================


def create_search_tools_function(backend: ToolSearchBackend) -> FunctionTool:
    """Create a ``search_tools`` FunctionTool backed by *backend*.

    The returned tool performs a catalog search and returns a JSON object
    with a ``results`` key (list of :class:`~tools.tool_search.ToolSearchResult`
    dicts) and an optional ``error`` key.  It is completely backend-agnostic —
    any object satisfying the :class:`ToolSearchBackend` protocol can be
    injected.

    Args:
        backend: A search backend implementing
                 :class:`~tools.tool_search.ToolSearchBackend`.

    Returns:
        ``FunctionTool`` named ``search_tools``.
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

    return FunctionTool(
        name="search_tools",
        description=(
            "Search the tool catalog for domain-specific tools by name or description.  "
            "Returns a JSON object with a 'results' array of matching tools (each with "
            "server_name, name, description, execution_type, and relevance score).  "
            "Domain tools are invoked programmatically inside execute_code — use search_tools "
            "to discover their names, signatures, and which server they belong to."
        ),
        approval_mode="never_require",
        func=search_tools,
        input_model=SearchToolsInput,
    )
