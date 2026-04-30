"""
Azure AI Search retrieval for tool-learning memory vignettes.

Provides hybrid (keyword + vector) search over the tool-vignettes index.
The index is populated by an Azure AI Search Table indexer with integrated
vectorization (skillset-driven embeddings).
"""

from __future__ import annotations

import logging
from typing import List, Optional

from azure.core.credentials import TokenCredential
from azure.search.documents import SearchClient
from azure.search.documents.models import VectorizableTextQuery, VectorQuery

from .config import ToolLearningConfig
from .models import Vignette

LOGGER = logging.getLogger(__name__)


def _escape_odata(value: str) -> str:
    """Escape a string value for safe inclusion in an OData filter literal."""
    return value.replace("'", "''")


def _build_scope_filter(
    tenant_id: Optional[str],
    user_id: Optional[str],
) -> str:
    """
    Build an OData filter string that enforces scope-based access control.

    Rules:
      - Always includes global-scoped vignettes (no tenant/user restriction).
      - If tenant_id is known, also includes org-scoped vignettes for that tenant.
      - If both tenant_id and user_id are known, also includes user-scoped vignettes.

    The filter returns documents matching ANY of the applicable scope levels.
    """
    clauses: list[str] = []

    # Escape values to prevent OData filter injection
    escaped_tenant_id = _escape_odata(tenant_id) if tenant_id is not None else None
    escaped_user_id = _escape_odata(user_id) if user_id is not None else None

    # Always include global-scoped vignettes
    clauses.append("scope eq 'global'")

    # Org-scoped vignettes (if tenant_id is known)
    if escaped_tenant_id:
        clauses.append(f"(scope eq 'org' and tenant_id eq '{escaped_tenant_id}')")

    # User-scoped vignettes (if both tenant and user are known)
    if escaped_tenant_id and escaped_user_id:
        clauses.append(f"(scope eq 'user' and tenant_id eq '{escaped_tenant_id}' and user_id eq '{escaped_user_id}')")

    return " or ".join(clauses)


class SearchVignetteRepo:
    """
    Azure AI Search repository for Vignette retrieval.

    Uses hybrid keyword + vector search against the tool-vignettes index.
    Integrated vectorization on the index handles query-time embedding.
    """

    def __init__(
        self,
        config: ToolLearningConfig,
        credential: Optional[TokenCredential] = None,
    ) -> None:
        """
        Initialize the search repository.

        Args:
            config: Agent memory configuration.
            credential: Azure TokenCredential (e.g. DefaultAzureCredential).
        """
        self._config = config
        self._client = self._create_client(credential)

    def _create_client(self, credential: Optional[TokenCredential]) -> SearchClient:
        if not self._config.search_endpoint:
            raise ValueError("TOOL_LEARNING_SEARCH_ENDPOINT must be set.")
        if credential is None:
            raise ValueError("A TokenCredential must be provided for AI Search access.")
        return SearchClient(
            endpoint=self._config.search_endpoint,
            index_name=self._config.search_index_name,
            credential=credential,
        )

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
        """
        Hybrid search for vignettes relevant to a tool call.

        Combines BM25 keyword scoring with vector similarity (via integrated
        vectorization on the index) and applies mandatory scope + tool filters.

        Args:
            query_text: Natural language query describing the tool call intent.
            tool_name: Name of the tool to filter on (mandatory).
            kind: Optional vignette kind filter.
            error_class: Optional error class filter (for repair template retrieval).
            tenant_id: Caller's tenant ID for scope filtering.
            user_id: Caller's user ID for scope filtering.
            top_k: Number of results to return (defaults to config.top_k).

        Returns:
            List of Vignette objects decoded from payload_json, sorted by relevance.
        """
        k = top_k if top_k is not None else self._config.top_k

        # Build mandatory filter — escape values to prevent OData injection
        filter_parts: list[str] = [f"tool_name eq '{_escape_odata(tool_name)}'"]

        if kind:
            filter_parts.append(f"kind eq '{_escape_odata(kind)}'")
        if error_class:
            filter_parts.append(f"error_class eq '{_escape_odata(error_class)}'")

        scope_filter = _build_scope_filter(tenant_id, user_id)
        filter_parts.append(f"({scope_filter})")

        odata_filter = " and ".join(filter_parts)

        # Hybrid search: keyword + vector via integrated vectorization
        vector_queries: list[VectorQuery] = [
            VectorizableTextQuery(
                text=query_text,
                k=k,
                fields="content_vector",
            )
        ]

        vignettes: List[Vignette] = []
        try:
            results = self._client.search(
                search_text=query_text,
                vector_queries=vector_queries,
                filter=odata_filter,
                top=k,
                select=["payload_json", "confidence", "updated_at"],
                order_by=["confidence desc", "updated_at desc"],
            )
            for result in results:
                payload_json = result.get("payload_json")
                if not payload_json:
                    continue
                try:
                    vignette = Vignette.model_validate_json(payload_json)
                    if vignette.confidence >= self._config.min_confidence:
                        vignettes.append(vignette)
                except Exception as e:
                    LOGGER.warning("Skipping malformed search result: %s", e)
        except Exception as e:
            raise RuntimeError(
                f"Azure AI Search query failed for tool '{tool_name}' "
                f"against index '{self._config.search_index_name}' "
                f"at {self._config.search_endpoint}: {e}"
            ) from e

        return vignettes[:k]
