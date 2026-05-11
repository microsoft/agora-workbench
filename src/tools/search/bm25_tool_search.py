"""
BM25-based tool search.

Exposes :class:`BM25ToolSearchBackend`, which satisfies the
:class:`~tools.tool_search.ToolSearchBackend` protocol for use with the
generic :func:`tools.search.core.create_search_tools_function` factory.
"""

import logging
from typing import Optional

from tools.tool_search import ToolSearchBackend, ToolSearchResult
from tools.search.build_tool_list import ToolInfo
from utilities.bm25 import BM25Index

LOGGER = logging.getLogger(__name__)


def _tool_info_text(tool: ToolInfo) -> str:
    """Derive the indexable text for a :class:`ToolInfo`."""
    return f"{tool.name} {tool.description} {' '.join(tool.affordances)}"


class BM25ToolSearchBackend(ToolSearchBackend):
    """BM25-based implementation of :class:`~tools.tool_search.ToolSearchBackend`.

    Builds a lightweight in-process BM25 index over :class:`ToolInfo` objects
    discovered from MCP servers via :func:`~tools.search.build_tool_list.build_tool_list`.

    Can be pre-loaded with a tools list, or will lazily discover tools
    on the first call to :meth:`search`.
    """

    def __init__(self, tools: Optional[list[ToolInfo]] = None):
        super().__init__()
        self._index: Optional[BM25Index[ToolInfo]] = None
        if tools is not None:
            self._index = BM25Index()
            for tool in tools:
                self._index.add(tool, _tool_info_text(tool))

    async def _ensure_index(self) -> BM25Index[ToolInfo]:
        """Build the BM25 index lazily if not pre-loaded."""
        if self._index is None:
            from tools.search.build_tool_list import build_tool_list

            tools = await build_tool_list()
            self._index = BM25Index()
            for tool in tools:
                self._index.add(tool, _tool_info_text(tool))
            LOGGER.info("BM25 tool search index built with %d tools", len(tools))
        return self._index

    async def search(self, query: str, top: int = 5) -> list[ToolSearchResult]:
        """Search the BM25 index for tools matching *query*."""
        index = await self._ensure_index()
        hits = index.search(query, top_k=top)
        results: list[ToolSearchResult] = []
        for tool_info, score in hits:
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


async def create_and_setup_bm25_tool_search() -> "BM25ToolSearchBackend":
    """Discover tools from MCP servers and return a ready BM25 backend.

    This is the BM25 counterpart to
    :func:`~tools.search.azure_ai_tool_search.create_and_setup_azure_ai_tool_search`.
    It calls :func:`~tools.search.build_tool_list.build_tool_list` to
    discover available tools and pre-populates the BM25 index.

    Returns:
        A :class:`BM25ToolSearchBackend` with the index already built.
    """
    from tools.search.build_tool_list import build_tool_list

    tools = await build_tool_list()
    backend = BM25ToolSearchBackend(tools)
    LOGGER.info("BM25 tool search ready with %d tools", len(tools))
    return backend
