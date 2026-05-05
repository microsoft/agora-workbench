"""
Azure AI Search-based tool search.

Required environment variables:
    TOOL_SEARCH_ENDPOINT: Azure AI Search service endpoint
                          (e.g. ``https://my-service.search.windows.net``)
    TOOL_SEARCH_VECTORIZER_ENDPOINT: Azure OpenAI endpoint for embeddings.
    TOOL_SEARCH_VECTORIZER_DEPLOYMENT: Embedding model deployment name.

The index name is generated programmatically per agent run by
``tools.search.manager.ToolSearchIndexManager``.

Use :func:`create_and_setup_azure_ai_tool_search` as the single entry-point:
it creates the ephemeral index, populates it, registers cleanup, and
returns the ``AzureAIToolSearchBackend`` + ``ToolSearchIndexManager``.
"""

import logging
import os
from typing import TYPE_CHECKING, Optional

import httpx

from azure.core.exceptions import HttpResponseError
from azure.search.documents.aio import SearchClient
from azure.search.documents.models import VectorizedQuery

from auth import get_search_credential_async
from tools.tool_search import ToolSearchBackend, ToolSearchResult
from .build_tool_list import build_tool_list
from ._constants import (
    TOOL_SEARCH_ENDPOINT_ENV,
    TOOL_SEARCH_VECTORIZER_ENDPOINT_ENV,
    TOOL_SEARCH_VECTORIZER_DEPLOYMENT_ENV,
    _OPENAI_EMBEDDING_API_VERSION,
)
from .manager import ToolSearchIndexManager

if TYPE_CHECKING:
    from azure.core.credentials_async import AsyncTokenCredential
    from .manager import ToolSearchIndexManager

LOGGER = logging.getLogger(__name__)


async def _embed_query(text: str, credential: "AsyncTokenCredential") -> list[float]:
    """Generate an embedding vector for *text* using Azure OpenAI (client-side).

    This bypasses the integrated vectorizer so we don't depend on Azure AI
    Search being able to authenticate to the OpenAI endpoint.

    Args:
        text: The text to embed.
        credential: An async Azure credential to obtain a bearer token.
    """
    endpoint = os.getenv(TOOL_SEARCH_VECTORIZER_ENDPOINT_ENV, "")
    deployment = os.getenv(TOOL_SEARCH_VECTORIZER_DEPLOYMENT_ENV, "")
    if not endpoint or not deployment:
        raise ValueError(
            f"Client-side embedding requires {TOOL_SEARCH_VECTORIZER_ENDPOINT_ENV} "
            f"and {TOOL_SEARCH_VECTORIZER_DEPLOYMENT_ENV}"
        )

    token_resp = await credential.get_token("https://cognitiveservices.azure.com/.default")

    # Strip any path/query already baked into the endpoint env var
    base = endpoint.split("/openai")[0].rstrip("/")
    url = f"{base}/openai/deployments/{deployment}/embeddings"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token_resp.token}",
    }
    params = {"api-version": _OPENAI_EMBEDDING_API_VERSION}

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, json={"input": [text]}, headers=headers, params=params)
    resp.raise_for_status()
    return resp.json()["data"][0]["embedding"]


# ============================================================================
# Search client manager
# ============================================================================


class ToolSearchClientManager:
    """
    Manages an Azure AI SearchClient for the tool index.

    Caches the client for performance and recreates it on authentication
    errors (e.g. expired tokens) so that long-running agents continue to work.

    Args:
        index_name: Name of the Azure AI Search index.
        credential: Optional async credential to use for the SearchClient.
            When provided, this credential is used directly and is **not**
            closed by the manager (the caller owns its lifecycle).  When
            ``None``, a default credential chain is created internally.
    """

    def __init__(self, index_name: str, credential: Optional["AsyncTokenCredential"] = None):
        self._index_name = index_name
        self._client: Optional[SearchClient] = None
        self._external_credential = credential is not None
        self._credential = credential  # lazily created when None

    @staticmethod
    def _get_endpoint() -> str:
        """
        Return the Azure AI Search endpoint from the environment.

        Raises:
            ValueError: If ``TOOL_SEARCH_ENDPOINT`` is not set.
        """
        endpoint = os.getenv(TOOL_SEARCH_ENDPOINT_ENV)
        if not endpoint:
            raise ValueError(f"Tool search not configured. Set {TOOL_SEARCH_ENDPOINT_ENV} environment variable.")
        return endpoint

    def _get_credential(self):
        """Return the cached async credential, creating it on first use."""
        if self._credential is None:
            self._credential = get_search_credential_async()
        return self._credential

    def _create_client(self) -> SearchClient:
        """Create a new SearchClient using the service credential chain."""
        endpoint = self._get_endpoint()
        return SearchClient(
            endpoint=endpoint,
            index_name=self._index_name,
            credential=self._get_credential(),
        )

    async def get_client(self) -> SearchClient:
        """Return the cached SearchClient, creating one if needed."""
        if self._client is None:
            self._client = self._create_client()
        return self._client

    async def search(self, **kwargs):
        """
        Execute a search with automatic retry on authentication errors.

        Args:
            **kwargs: Forwarded to ``SearchClient.search()``.

        Returns:
            Async iterator of search results.
        """
        try:
            client = await self.get_client()
            return await client.search(**kwargs)
        except HttpResponseError as exc:
            if hasattr(exc, "status_code") and exc.status_code in (401, 403):
                LOGGER.info("Tool search auth error; recreating client with fresh credentials")
                await self._reset_client()
                client = await self.get_client()
                return await client.search(**kwargs)
            raise

    async def _reset_client(self) -> None:
        """Close and discard the cached client."""
        if self._client:
            try:
                await self._client.close()
            except Exception:
                LOGGER.debug("Error closing SearchClient", exc_info=True)
            self._client = None

    async def close(self) -> None:
        """Close the SearchClient and internally-created credential, releasing resources."""
        await self._reset_client()
        # Only close the credential if we created it ourselves.
        if self._credential is not None and not self._external_credential:
            try:
                await self._credential.close()
            except Exception:
                LOGGER.debug("Error closing credential", exc_info=True)
            self._credential = None


# ============================================================================
# AzureAIToolSearchBackend — implements ToolSearchBackend protocol
# ============================================================================


class AzureAIToolSearchBackend(ToolSearchBackend):
    """Azure AI Search implementation of :class:`~tools.tool_search.ToolSearchBackend`.

    Performs hybrid retrieval (keyword + vector + semantic reranker) with
    automatic fallback to full-text search when advanced features are
    unavailable.

    Args:
        index_name: Name of the Azure AI Search index to query.
    """

    def __init__(self, index_name: str):
        super().__init__()
        self._credential = get_search_credential_async()
        self._client_manager = ToolSearchClientManager(index_name, credential=self._credential)

    async def search(self, query: str, top: int = 5) -> list[ToolSearchResult]:
        """Search the Azure AI Search index for tools matching *query*."""
        # Generate query embedding
        query_vector = await _embed_query(query, self._credential)

        search_kwargs: dict = {
            "search_text": query,
            "top": top,
            "query_type": "semantic",
            "semantic_configuration_name": "default-semantic-config",
            "vector_queries": [
                VectorizedQuery(
                    vector=query_vector,
                    k=max(top * 3, 5),
                    fields="description_vector",
                ),
            ],
        }

        try:
            results_iter = await self._client_manager.search(**search_kwargs)
        except HttpResponseError as exc:
            error_msg = str(exc).lower()
            if "semantic" in error_msg or "vector" in error_msg:
                LOGGER.warning("Semantic/vector search unavailable; falling back to full-text search")
                results_iter = await self._client_manager.search(
                    search_text=query,
                    top=top,
                )
            else:
                raise

        results: list[ToolSearchResult] = []
        async for item in results_iter:
            d = dict(item)
            results.append(
                ToolSearchResult(
                    name=d.get("name", ""),
                    server_name=d.get("server_name", ""),
                    description=d.get("description", ""),
                    execution_type=d.get("execution_type", ""),
                    score=d.get("@search.score"),
                    state_requires=d.get("state_requires", []),
                    state_produces=d.get("state_produces", []),
                )
            )
        return results

    async def close(self) -> None:
        """Release resources held by the client manager and credential."""
        await self._client_manager.close()
        if self._credential is not None:
            try:
                await self._credential.close()
            except Exception:
                LOGGER.debug("Error closing AzureAIToolSearchBackend credential", exc_info=True)
            self._credential = None


# ============================================================================
# Combined factory: index lifecycle + tool creation
# ============================================================================


async def create_and_setup_azure_ai_tool_search() -> tuple["AzureAIToolSearchBackend", "ToolSearchIndexManager"]:
    """
    Create an ephemeral Azure AI Search index and return a backend + manager.

    This is the single entry-point that couples index lifecycle management
    with backend creation.  It:

    1. Discovers tools from MCP servers via :func:`build_tool_list`.
    2. Creates a :class:`ToolSearchIndexManager` from environment variables.
    3. Deploys a uniquely-named index, populates it from the discovered tools,
       and registers ``atexit``/signal cleanup handlers.
    3. Builds and returns an :class:`AzureAIToolSearchBackend` that queries
       the newly created index.

    The returned *manager* is provided so the caller can explicitly call
    ``await manager.delete_index()`` during graceful shutdown.

    Callers feed the backend into :func:`tools.search.core.create_search_tools_function`
    and :func:`tools.search.core.create_search_tools_function` separately.

    Returns:
        A ``(AzureAIToolSearchBackend, ToolSearchIndexManager)`` tuple.

    Raises:
        ValueError: If required environment variables are not set.
        httpx.HTTPStatusError: If index deployment or population fails.
    """

    tools = await build_tool_list()
    manager = ToolSearchIndexManager.from_env()
    await manager.setup(tools)

    backend = AzureAIToolSearchBackend(index_name=manager.index_name)

    LOGGER.info(
        "Tool search ready: index '%s' with %d tools",
        manager.index_name,
        len(tools),
    )

    return backend, manager
