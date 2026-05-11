"""Read-repository protocol for vignette retrieval backends.

Defines :class:`VignetteSearchRepo`, the structural interface that
:class:`~.search_repo.SearchVignetteRepo` (Azure AI Search) and
:class:`~.local_file_search_repo.LocalFileSearchVignetteRepo` (local BM25
over JSON files) both satisfy.
"""

from __future__ import annotations

from typing import List, Optional, Protocol

from .models import Vignette


class VignetteSearchRepo(Protocol):
    """Retrieval interface for vignette search backends."""

    def search_vignettes(
        self,
        query_text: str,
        tool_name: str,
        kind: Optional[str] = None,
        error_class: Optional[str] = None,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        top_k: Optional[int] = None,
    ) -> List[Vignette]:
        """Retrieve vignettes relevant to a tool call.

        Args:
            query_text: Natural language query describing intent.
            tool_name: Tool to filter on (mandatory).
            kind: Optional vignette kind filter (``"anti_pattern"`` /
                ``"repair_template"``).
            error_class: Optional error class filter.
            tenant_id: Caller tenant for scope filtering.
            user_id: Caller user for scope filtering.
            top_k: Number of results to return (defaults to backend setting).

        Returns:
            List of matching :class:`~.models.Vignette` ordered by relevance
            (highest first).
        """
        ...
