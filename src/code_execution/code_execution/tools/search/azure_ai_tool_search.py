"""Persistent Azure AI Search backend for server-side tool search."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import os
import re
from typing import Any

import httpx
from azure.search.documents.aio import SearchClient
from azure.search.documents.indexes.aio import SearchIndexClient
from azure.search.documents.indexes.models import (
    HnswAlgorithmConfiguration,
    SearchField,
    SearchFieldDataType,
    SearchIndex,
    SearchableField,
    SemanticConfiguration,
    SemanticField,
    SemanticPrioritizedFields,
    SemanticSearch,
    SimpleField,
    VectorSearch,
    VectorSearchProfile,
)
from azure.search.documents.models import VectorizedQuery

from utilities.auth import BearerTokenAuth, get_search_credential_async, get_token_provider
from utilities.tool_search import ToolInfo, ToolSearchBackend, ToolSearchResult

from ._constants import (
    TOOL_SEARCH_ENDPOINT_ENV,
    TOOL_SEARCH_VECTORIZER_DEPLOYMENT_ENV,
    TOOL_SEARCH_VECTORIZER_ENDPOINT_ENV,
    _OPENAI_EMBEDDING_API_VERSION,
)

LOGGER = logging.getLogger(__name__)

_DEFAULT_AOAI_SCOPE = "https://cognitiveservices.azure.com/.default"
_SEMANTIC_CONFIGURATION_NAME = "default-semantic-config"
_VECTOR_PROFILE_NAME = "tool-vector-profile"
_VECTOR_ALGORITHM_NAME = "tool-vector-algorithm"
_SELECT_FIELDS = [
    "id",
    "name",
    "server_name",
    "description",
    "affordances",
    "state_requires",
    "state_produces",
]


def _tool_document_id(tool: ToolInfo) -> str:
    """Derive a stable document ID for a tool."""
    return hashlib.sha256(f"{tool.server_name}:{tool.name}".encode("utf-8")).hexdigest()


def _tool_embedding_text(tool: ToolInfo) -> str:
    """Build the text used to embed a tool document."""
    parts = [tool.name, tool.description]
    if tool.affordances:
        parts.append("Affordances: " + ", ".join(tool.affordances))
    if tool.state_requires:
        parts.append("Requires: " + ", ".join(tool.state_requires))
    if tool.state_produces:
        parts.append("Produces: " + ", ".join(tool.state_produces))
    return "\n".join(part for part in parts if part)


def _sanitize_index_name(server_name: str) -> str:
    """Normalize a server name into a valid Azure AI Search index name."""
    normalized = re.sub(r"[^a-z0-9-]", "-", server_name.lower())
    normalized = re.sub(r"-+", "-", normalized).strip("-")
    if not normalized:
        normalized = "default"
    base = f"tool-search-{normalized}"
    if len(base) <= 128:
        return base
    suffix = hashlib.sha256(server_name.encode("utf-8")).hexdigest()[:8]
    return f"{base[:119].rstrip('-')}-{suffix}"


class AzureAIToolSearchBackend(ToolSearchBackend):
    """Azure AI Search implementation of ToolSearchBackend.

    Creates a persistent search index per server and performs hybrid
    (keyword + vector + semantic) retrieval with automatic fallback.

    Required environment variables:
        TOOL_SEARCH_ENDPOINT: Azure AI Search endpoint
        TOOL_SEARCH_VECTORIZER_ENDPOINT: Azure OpenAI endpoint
        TOOL_SEARCH_VECTORIZER_DEPLOYMENT: Embedding deployment name
    """

    def __init__(
        self,
        tools: list[ToolInfo],
        server_name: str = "",
        index_name: str | None = None,
        endpoint: str | None = None,
    ):
        super().__init__()
        derived_server_name = server_name or (tools[0].server_name if tools else "default")
        self._tools = tools
        self.server_name = derived_server_name
        self.index_name = index_name or _sanitize_index_name(derived_server_name)
        self.endpoint = endpoint or os.getenv(TOOL_SEARCH_ENDPOINT_ENV)
        self._credential = get_search_credential_async()
        self._index_client: SearchIndexClient | None = None
        self._search_client: SearchClient | None = None
        self._http_client: httpx.AsyncClient | None = None
        self._initialized = False

    async def initialize(self) -> None:
        """Create/update the index and upload tool documents. Call once at startup."""
        if self._initialized:
            return

        if not self.endpoint:
            raise ValueError(f"Azure AI Search endpoint is required; set {TOOL_SEARCH_ENDPOINT_ENV} or pass endpoint=.")

        documents = await self._build_documents()
        embedding_dimensions = (
            len(documents[0]["description_vector"]) if documents else len(await self._embed_text("tool search"))
        )

        self._index_client = SearchIndexClient(endpoint=self.endpoint, credential=self._credential)
        await self._index_client.create_or_update_index(self._build_index(embedding_dimensions))

        self._search_client = SearchClient(
            endpoint=self.endpoint,
            index_name=self.index_name,
            credential=self._credential,
        )
        if documents:
            await self._search_client.merge_or_upload_documents(documents=documents)

        self._initialized = True
        LOGGER.info("Azure AI Search tool index '%s' synced with %d tools", self.index_name, len(self._tools))

    async def search(self, query: str, top: int = 5) -> list[ToolSearchResult]:
        """Search the persistent Azure AI Search tool index."""
        if top <= 0:
            return []
        if not self._initialized:
            await self.initialize()
        if self._search_client is None:
            return []

        search_text = query.strip() or "*"

        if query.strip():
            try:
                query_vector = await self._embed_text(query)
                results = await self._search_client.search(
                    search_text=search_text,
                    semantic_query=query,
                    query_type="semantic",
                    semantic_configuration_name=_SEMANTIC_CONFIGURATION_NAME,
                    select=_SELECT_FIELDS,
                    top=top,
                    vector_queries=[
                        VectorizedQuery(
                            vector=query_vector,
                            fields="description_vector",
                            k=top,
                        )
                    ],
                )
                return await self._collect_results(results)
            except Exception as exc:
                LOGGER.warning(
                    "Hybrid Azure AI Search failed for index '%s'; falling back to keyword-only search: %s",
                    self.index_name,
                    exc,
                )

        results = await self._search_client.search(
            search_text=search_text,
            select=_SELECT_FIELDS,
            top=top,
        )
        return await self._collect_results(results)

    async def close(self) -> None:
        """Close network clients without deleting the persistent index."""
        await self._close_resource(self._search_client)
        await self._close_resource(self._index_client)
        await self._close_resource(self._http_client)
        await self._close_resource(self._credential)
        self._search_client = None
        self._index_client = None
        self._http_client = None
        self._initialized = False

    def _build_index(self, embedding_dimensions: int) -> SearchIndex:
        """Construct the Azure AI Search index schema."""
        fields = [
            SimpleField(name="id", type=SearchFieldDataType.String, key=True, filterable=True),
            SearchableField(name="name", sortable=True),
            SearchableField(name="server_name", filterable=True, sortable=True),
            SearchableField(name="description"),
            SearchableField(name="affordances", collection=True),
            SearchableField(name="state_requires", collection=True, filterable=True),
            SearchableField(name="state_produces", collection=True, filterable=True),
            SearchField(
                name="description_vector",
                type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
                searchable=True,
                hidden=True,
                vector_search_dimensions=embedding_dimensions,
                vector_search_profile_name=_VECTOR_PROFILE_NAME,
            ),
        ]

        return SearchIndex(
            name=self.index_name,
            fields=fields,
            semantic_search=SemanticSearch(
                default_configuration_name=_SEMANTIC_CONFIGURATION_NAME,
                configurations=[
                    SemanticConfiguration(
                        name=_SEMANTIC_CONFIGURATION_NAME,
                        prioritized_fields=SemanticPrioritizedFields(
                            title_field=SemanticField(field_name="name"),
                            content_fields=[SemanticField(field_name="description")],
                            keywords_fields=[
                                SemanticField(field_name="affordances"),
                                SemanticField(field_name="state_requires"),
                                SemanticField(field_name="state_produces"),
                            ],
                        ),
                    )
                ],
            ),
            vector_search=VectorSearch(
                profiles=[
                    VectorSearchProfile(
                        name=_VECTOR_PROFILE_NAME,
                        algorithm_configuration_name=_VECTOR_ALGORITHM_NAME,
                    )
                ],
                algorithms=[HnswAlgorithmConfiguration(name=_VECTOR_ALGORITHM_NAME)],
            ),
        )

    async def _build_documents(self) -> list[dict[str, Any]]:
        """Embed and serialize tool metadata for indexing."""
        if not self._tools:
            return []

        embeddings = await asyncio.gather(*(self._embed_text(_tool_embedding_text(tool)) for tool in self._tools))
        return [
            {
                "id": _tool_document_id(tool),
                "name": tool.name,
                "server_name": tool.server_name,
                "description": tool.description,
                "affordances": list(tool.affordances),
                "state_requires": list(tool.state_requires),
                "state_produces": list(tool.state_produces),
                "description_vector": embedding,
            }
            for tool, embedding in zip(self._tools, embeddings, strict=True)
        ]

    async def _embed_text(self, text: str) -> list[float]:
        """Generate an embedding for *text* using Azure OpenAI over HTTP."""
        deployment = os.getenv(TOOL_SEARCH_VECTORIZER_DEPLOYMENT_ENV)
        if not deployment:
            raise ValueError(f"Embedding deployment is required; set {TOOL_SEARCH_VECTORIZER_DEPLOYMENT_ENV}.")

        client = self._get_http_client()
        response = await client.post(
            self._embedding_url(),
            json={"input": text},
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data", [])
        if not data:
            raise ValueError("Azure OpenAI embeddings response did not contain any vectors.")
        embedding = data[0].get("embedding")
        if not isinstance(embedding, list) or not embedding:
            raise ValueError("Azure OpenAI embeddings response contained an invalid vector.")
        return [float(value) for value in embedding]

    def _embedding_url(self) -> str:
        """Resolve the configured embedding endpoint."""
        endpoint = os.getenv(TOOL_SEARCH_VECTORIZER_ENDPOINT_ENV)
        deployment = os.getenv(TOOL_SEARCH_VECTORIZER_DEPLOYMENT_ENV)
        if not endpoint:
            raise ValueError(f"Embedding endpoint is required; set {TOOL_SEARCH_VECTORIZER_ENDPOINT_ENV}.")
        if not deployment:
            raise ValueError(f"Embedding deployment is required; set {TOOL_SEARCH_VECTORIZER_DEPLOYMENT_ENV}.")

        if "/openai/deployments/" in endpoint and "/embeddings" in endpoint:
            if "api-version=" in endpoint:
                return endpoint
            separator = "&" if "?" in endpoint else "?"
            return f"{endpoint}{separator}api-version={_OPENAI_EMBEDDING_API_VERSION}"

        base_endpoint = endpoint.rstrip("/")
        return f"{base_endpoint}/openai/deployments/{deployment}/embeddings?api-version={_OPENAI_EMBEDDING_API_VERSION}"

    def _get_http_client(self) -> httpx.AsyncClient:
        """Create the shared HTTP client used for embeddings."""
        if self._http_client is None:
            api_key = os.getenv("AZURE_OPENAI_API_KEY")
            headers = {"Content-Type": "application/json"}
            auth = None
            if api_key:
                headers["api-key"] = api_key
            else:
                scope = os.getenv("AOAI_SCOPE", _DEFAULT_AOAI_SCOPE)
                auth = BearerTokenAuth(get_token_provider(scope))
            self._http_client = httpx.AsyncClient(
                auth=auth,
                headers=headers,
                timeout=httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0),
            )
        return self._http_client

    async def _collect_results(self, results: Any) -> list[ToolSearchResult]:
        """Convert Azure search documents into ToolSearchResult objects."""
        output: list[ToolSearchResult] = []
        async for document in results:
            output.append(
                ToolSearchResult(
                    name=str(document.get("name", "")),
                    server_name=str(document.get("server_name", "")),
                    description=str(document.get("description", "")),
                    execution_type="mcp",
                    score=self._extract_score(document),
                    state_requires=[str(value) for value in document.get("state_requires", [])],
                    state_produces=[str(value) for value in document.get("state_produces", [])],
                )
            )
        return output

    @staticmethod
    def _extract_score(document: dict[str, Any]) -> float | None:
        """Extract the most useful score field from a search hit."""
        for key in ("@search.reranker_score", "@search.score"):
            value = document.get(key)
            if value is not None:
                return float(value)
        return None

    @staticmethod
    async def _close_resource(resource: Any) -> None:
        """Close a resource if it exposes a close() method."""
        if resource is None:
            return
        close = getattr(resource, "close", None)
        if close is None:
            return
        result = close()
        if inspect.isawaitable(result):
            await result
