"""
BM25-based tool search.

Exposes :class:`BM25ToolSearchBackend`, which satisfies the
:class:`~tools.tool_search.ToolSearchBackend` protocol for use with the
generic :func:`tools.search.core.create_search_tools_function` factory.
"""

import logging
import math
import re
from typing import Optional

from tools.tool_search import ToolSearchBackend, ToolSearchResult
from tools.search.build_tool_list import ToolInfo

LOGGER = logging.getLogger(__name__)


# ============================================================================
# BM25 Implementation
# ============================================================================


def tokenize(text: str) -> list[str]:
    """Simple whitespace + punctuation tokenizer with lowercasing."""
    return re.findall(r"[a-z0-9_]+", text.lower())


def _tokenize(text: str) -> list[str]:
    """Backward-compatible alias for internal callers."""
    return tokenize(text)


class BM25Index:
    """
    Lightweight BM25 (Okapi BM25) index over tool info objects.

    Indexes on tool name + description + affordances. Supports incremental additions.
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self._docs: list[tuple[ToolInfo, list[str]]] = []  # (tool_info, tokens)
        self._df: dict[str, int] = {}  # document frequency per term
        self._avgdl: float = 0.0

    def add(self, tool_info: ToolInfo) -> None:
        """Add a tool info entry to the index."""
        text = f"{tool_info.name} {tool_info.description} {' '.join(tool_info.affordances)}"
        tokens = _tokenize(text)
        self._docs.append((tool_info, tokens))

        # Update document frequencies
        seen = set()
        for token in tokens:
            if token not in seen:
                self._df[token] = self._df.get(token, 0) + 1
                seen.add(token)

        # Recompute average document length
        total_tokens = sum(len(toks) for _, toks in self._docs)
        self._avgdl = total_tokens / len(self._docs) if self._docs else 0.0

    def search(self, query: str, top_k: int = 1) -> list[tuple[ToolInfo, float]]:
        """
        Search for tools matching the query.

        Args:
            query: Natural language search query
            top_k: Number of top results to return

        Returns:
            List of (ToolInfo, score) tuples sorted by descending score
        """
        if not self._docs:
            return []

        query_tokens = _tokenize(query)
        if not query_tokens:
            return []

        n = len(self._docs)
        scores: list[tuple[ToolInfo, float]] = []

        for tool_info, doc_tokens in self._docs:
            score = 0.0
            dl = len(doc_tokens)

            # Build term frequency map for this document
            tf_map: dict[str, int] = {}
            for token in doc_tokens:
                tf_map[token] = tf_map.get(token, 0) + 1

            for qt in query_tokens:
                if qt not in self._df:
                    continue
                df = self._df[qt]
                tf = tf_map.get(qt, 0)
                if tf == 0:
                    continue

                # IDF component (BM25 variant)
                idf = math.log((n - df + 0.5) / (df + 0.5) + 1.0)

                # TF component with length normalization
                if self._avgdl == 0:
                    # Edge case: no tokens across all documents, fall back to
                    # BM25 formula without length normalization.
                    tf_norm = (tf * (self.k1 + 1)) / (tf + self.k1)
                else:
                    tf_norm = (tf * (self.k1 + 1)) / (tf + self.k1 * (1 - self.b + self.b * dl / self._avgdl))

                score += idf * tf_norm

            scores.append((tool_info, score))

        # Sort by score descending
        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:top_k]


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
