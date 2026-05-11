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
from utilities.bm25 import BM25Index as _GenericBM25Index, tokenize

LOGGER = logging.getLogger(__name__)


# Backward-compatible alias kept for callers that imported the private name.
_tokenize = tokenize


# ============================================================================
# BM25 Implementation — ToolInfo-specific wrapper around the generic index
# ============================================================================


class BM25Index:
    """BM25 index keyed on :class:`ToolInfo` documents.

    Thin wrapper around :class:`utilities.bm25.BM25Index` that knows
    how to derive the indexable text from a ``ToolInfo`` (name +
    description + affordances). Kept as a public class for backward
    compatibility with existing callers and tests.
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self._index: _GenericBM25Index[ToolInfo] = _GenericBM25Index(k1=k1, b=b)

    @property
    def k1(self) -> float:
        return self._index.k1

    @property
    def b(self) -> float:
        return self._index.b

    # Internal-state accessors. Kept as properties (not attributes) so the
    # wrapper stays a thin façade over the generic index. Existing tests
    # poke these directly.
    @property
    def _docs(self):
        return self._index._docs

    @property
    def _df(self):
        return self._index._df

    @property
    def _avgdl(self) -> float:
        return self._index._avgdl

    def add(self, tool_info: ToolInfo) -> None:
        """Add a tool info entry to the index."""
        text = f"{tool_info.name} {tool_info.description} {' '.join(tool_info.affordances)}"
        self._index.add(tool_info, text)

    def search(self, query: str, top_k: int = 1) -> list[tuple[ToolInfo, float]]:
        """Search for tools matching the query.

        Args:
            query: Natural language search query
            top_k: Number of top results to return

        Returns:
            List of ``(ToolInfo, score)`` tuples sorted by descending score.
        """
        return self._index.search(query, top_k=top_k)


# ============================================================================
# BM25ToolSearchBackend — implements ToolSearchBackend protocol
# ============================================================================


class BM25ToolSearchBackend(ToolSearchBackend):
    """BM25-based implementation of :class:`~tools.tool_search.ToolSearchBackend`.

    Builds a lightweight in-process BM25 index over :class:`ToolInfo` objects
    discovered from MCP servers via :func:`~tools.search.build_tool_list.build_tool_list`.

    Can be pre-loaded with a tools list, or will lazily discover tools
    on the first call to :meth:`search`.
    """

    def __init__(self, tools: Optional[list[ToolInfo]] = None):
        super().__init__()
        self._index: Optional[BM25Index] = None
        if tools is not None:
            self._index = BM25Index()
            for tool in tools:
                self._index.add(tool)

    async def _ensure_index(self) -> BM25Index:
        """Build the BM25 index lazily if not pre-loaded."""
        if self._index is None:
            from tools.search.build_tool_list import build_tool_list

            tools = await build_tool_list()
            self._index = BM25Index()
            for tool in tools:
                self._index.add(tool)
            LOGGER.info("BM25 tool search index built with %d tools", len(tools))
        return self._index

    async def search(self, query: str, top: int = 5) -> list[ToolSearchResult]:
        """Search the BM25 index for tools matching *query*."""
        index = await self._ensure_index()
        hits = index.search(query, top_k=top)
        results: list[ToolSearchResult] = []
        for tool_info, score in hits:
            execution_type = "mcp"
            results.append(
                ToolSearchResult(
                    name=tool_info.name,
                    server_name=tool_info.server_name,
                    description=tool_info.description,
                    execution_type=execution_type,
                    score=score,
                    state_requires=list(tool_info.state_requires),
                    state_produces=list(tool_info.state_produces),
                )
            )
        return results


# ============================================================================
# Factory: build_tool_list + BM25 backend creation
# ============================================================================


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
