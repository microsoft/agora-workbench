"""
BM25-based tool search.

Exposes :class:`BM25ToolSearchBackend`, a :class:`~utilities.tool_search.ToolSearchBackend`
implementation backed by an in-process BM25 index.  The index is built
eagerly from a list of :class:`~utilities.tool_search.ToolInfo` objects at
construction time, making it suitable for server-side use where the tool
catalog is known at startup.
"""

import logging
from typing import Any

from utilities.tool_search import (
    SearchCategory,
    ToolInfo,
    ToolSearchBackend,
    ToolSearchResult,
)
from utilities.bm25 import BM25Index

LOGGER = logging.getLogger(__name__)


def _tool_info_text(tool: ToolInfo) -> str:
    """Derive the indexable text for a :class:`ToolInfo`."""
    return f"{tool.name} {tool.description} {' '.join(tool.affordances)}"


def _skill_info_text(skill: dict[str, Any]) -> str:
    """Derive the indexable text for a skill metadata dict."""
    parts = [skill.get("name", ""), skill.get("description", "")]
    states = skill.get("states", [])
    if states:
        parts.extend(states)
    return " ".join(parts)


class BM25ToolSearchBackend(ToolSearchBackend):
    """BM25-based implementation of :class:`~utilities.tool_search.ToolSearchBackend`.

    Builds lightweight in-process BM25 indexes over tools and skills at
    construction time.  Separate indexes ensure that ``category`` filtering
    does not interfere with ``top`` ranking.

    Args:
        tools: Pre-computed list of tool metadata to index.
        skills: Optional list of skill metadata dicts (from ``_discover_skills``).
        server_name: Server name used to generate ``to_access`` instructions.
    """

    def __init__(
        self,
        tools: list[ToolInfo],
        skills: list[dict[str, Any]] | None = None,
        server_name: str = "",
    ):
        super().__init__()
        self._server_name = server_name
        self._tool_index: BM25Index[ToolInfo] = BM25Index()
        self._skill_index: BM25Index[dict[str, Any]] = BM25Index()

        for tool in tools:
            self._tool_index.add(tool, _tool_info_text(tool))

        for skill in skills or []:
            self._skill_index.add(skill, _skill_info_text(skill))

        LOGGER.debug(
            "BM25ToolSearchBackend index built with %d tools and %d skills",
            len(tools),
            len(skills or []),
        )

    async def search(self, query: str, top: int = 5, category: SearchCategory = "all") -> list[ToolSearchResult]:
        """Search the BM25 index for tools and/or skills matching *query*.

        Returns only results with a positive BM25 score; zero-score matches
        are excluded.  When ``category="all"``, returns up to *top* results
        from each category (tools and skills), merged and sorted by score.
        """
        results: list[ToolSearchResult] = []

        if category in ("all", "tools"):
            tool_hits = self._tool_index.search(query, top_k=top)
            for tool_info, score in tool_hits:
                if score <= 0:
                    continue
                to_access = f"Call via execute_{self._server_name}_code" if self._server_name else ""
                results.append(
                    ToolSearchResult(
                        name=tool_info.name,
                        server_name=tool_info.server_name,
                        description=tool_info.description,
                        execution_type="mcp",
                        type="tool",
                        to_access=to_access,
                        score=score,
                        state_requires=list(tool_info.state_requires),
                        state_produces=list(tool_info.state_produces),
                    )
                )

        if category in ("all", "skills"):
            skill_hits = self._skill_index.search(query, top_k=top)
            for skill_info, score in skill_hits:
                if score <= 0:
                    continue
                skill_name = skill_info.get("name", "")
                to_access = (
                    f'Load with load_{self._server_name}_skill(skill_name="{skill_name}")' if self._server_name else ""
                )
                results.append(
                    ToolSearchResult(
                        name=skill_name,
                        server_name=self._server_name,
                        description=skill_info.get("description", ""),
                        execution_type="skill",
                        type="skill",
                        to_access=to_access,
                        score=score,
                        state_requires=[],
                        state_produces=[],
                    )
                )

        # Sort merged results by score descending
        results.sort(key=lambda r: r.score or 0, reverse=True)
        return results
