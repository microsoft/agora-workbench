"""
BM25-based tool search.

Exposes :class:`BM25ToolSearchBackend`, a :class:`~utilities.tool_search.ToolSearchBackend`
implementation backed by an in-process BM25 index.  The index is built
eagerly from a list of :class:`~utilities.tool_search.ToolInfo` objects at
construction time, making it suitable for server-side use where the tool
catalog is known at startup.
"""

import logging

from utilities.tool_search import ToolInfo, ToolSearchBackend, ToolSearchResult
from utilities.bm25 import BM25Index

LOGGER = logging.getLogger(__name__)


def _tool_info_text(tool: ToolInfo) -> str:
    """Derive the indexable text for a :class:`ToolInfo`."""
    return f"{tool.name} {tool.description} {' '.join(tool.affordances)}"


class BM25ToolSearchBackend(ToolSearchBackend):
    """BM25-based implementation of :class:`~utilities.tool_search.ToolSearchBackend`.

    Builds a lightweight in-process BM25 index over :class:`~utilities.tool_search.ToolInfo`
    objects at construction time.  Pass an empty list to create a no-op backend.

    Args:
        tools: Pre-computed list of tool metadata to index.  The server
               supplies its own tool list directly at startup.
    """

    def __init__(self, tools: list[ToolInfo]):
        super().__init__()
        self._index: BM25Index[ToolInfo] = BM25Index()
        for tool in tools:
            self._index.add(tool, _tool_info_text(tool))
        LOGGER.debug("BM25ToolSearchBackend index built with %d tools", len(tools))

    async def search(self, query: str, top: int = 5) -> list[ToolSearchResult]:
        """Search the BM25 index for tools matching *query*.

        Returns only results with a positive BM25 score; zero-score matches
        (i.e. no query tokens appear in any tool) are excluded.
        """
        hits = self._index.search(query, top_k=top)
        results: list[ToolSearchResult] = []
        for tool_info, score in hits:
            if score <= 0:
                continue
            results.append(
                ToolSearchResult(
                    name=tool_info.name,
                    server_name=tool_info.server_name,
                    description=tool_info.description,
                    execution_type="mcp",
                    score=score,
                    state_requires=list(tool_info.state_requires),
                    state_produces=list(tool_info.state_produces),
                )
            )
        return results
