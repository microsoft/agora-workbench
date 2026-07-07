"""Ephemeral Azure AI Search backend for server-side tool search."""

from __future__ import annotations

import atexit
import asyncio
import functools
import hashlib
import inspect
import logging
import os
import re
import uuid
from typing import Any

import httpx
from azure.core.exceptions import ResourceNotFoundError
from azure.search.documents.aio import SearchClient
from azure.search.documents.indexes import SearchIndexClient as SyncSearchIndexClient
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

from ...auth import (
    BearerTokenAuth,
    get_search_credential,
    get_search_credential_async,
    get_token_provider,
)
from ..tool_search import ToolInfo, ToolSearchBackend, ToolSearchResult, SearchCategory

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
    "type",
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


def _skill_document_id(skill: dict[str, Any]) -> str:
    """Derive a stable document ID for a skill."""
    domain = skill.get("domain", "")
    name = skill.get("name", "")
    return hashlib.sha256(f"skill:{domain}:{name}".encode("utf-8")).hexdigest()


def _skill_embedding_text(skill: dict[str, Any]) -> str:
    """Build the text used to embed a skill document."""
    parts = [skill.get("name", ""), skill.get("description", "")]
    states = skill.get("states", [])
    if states:
        parts.append("States: " + ", ".join(states))
    return "\n".join(part for part in parts if part)


def _sanitize_index_name(server_name: str) -> str:
    """Normalize a server name into a valid Azure AI Search index slug."""
    normalized = re.sub(r"[^a-z0-9-]", "-", server_name.lower())
    normalized = re.sub(r"-+", "-", normalized).strip("-")
    return normalized or "default"


def _build_ephemeral_index_name(server_name: str) -> str:
    """Build a unique per-instance Azure AI Search index name."""
    base = f"tool-search-{_sanitize_index_name(server_name)}"
    suffix = uuid.uuid4().hex[:8]
    if len(base) > 119:
        base = base[:119].rstrip("-")
    return f"{base}-{suffix}"


def _delete_index_at_exit(endpoint: str, index_name: str) -> None:
    """Best-effort synchronous index deletion for interpreter shutdown."""
    credential = get_search_credential()
    client = SyncSearchIndexClient(endpoint=endpoint, credential=credential)
    try:
        client.delete_index(index_name)
    except ResourceNotFoundError:
        return
    except Exception:
        LOGGER.warning("Failed to delete Azure AI Search index '%s' during interpreter shutdown", index_name)
    finally:
        client.close()
        close_credential = getattr(credential, "close", None)
        if callable(close_credential):
            close_credential()


class AzureAIToolSearchBackend(ToolSearchBackend):
    """Azure AI Search implementation of ToolSearchBackend.

    Creates an ephemeral search index per server instance and performs hybrid
    (keyword + vector + semantic) retrieval with automatic fallback.

    Required environment variables:
        TOOL_SEARCH_ENDPOINT: Azure AI Search endpoint
        TOOL_SEARCH_VECTORIZER_ENDPOINT: Azure OpenAI endpoint
        TOOL_SEARCH_VECTORIZER_DEPLOYMENT: Embedding deployment name
    """

    def __init__(
        self,
        index_name: str | None = None,
        endpoint: str | None = None,
    ):
        super().__init__()
        self._tools: list[ToolInfo] = []
        self._skills: list[dict[str, Any]] = []
        self.server_name: str = ""
        self.index_name = index_name or ""
        self.endpoint = endpoint or os.getenv(TOOL_SEARCH_ENDPOINT_ENV)
        self._credential = get_search_credential_async()
        self._index_client: SearchIndexClient | None = None
        self._search_client: SearchClient | None = None
        self._http_client: httpx.AsyncClient | None = None
        self._http_client_lock = asyncio.Lock()
        self._atexit_cleanup: Any | None = None
        self._index_created = False
        self._initialized = False

    def index(self, tools: list[ToolInfo], skills: list[dict[str, Any]] | None = None, server_name: str = "") -> None:
        """Store tool and skill metadata for later indexing during initialize()."""
        self._tools = tools
        self._skills = skills or []
        self.server_name = server_name or (tools[0].server_name if tools else "default")
        if not self.index_name:
            self.index_name = _build_ephemeral_index_name(self.server_name)

    async def initialize(self) -> None:
        """Create a fresh index and upload tool documents. Call once at startup."""
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
        self._index_created = True
        self._register_atexit_cleanup()

        self._search_client = SearchClient(
            endpoint=self.endpoint,
            index_name=self.index_name,
            credential=self._credential,
        )
        if documents:
            await self._search_client.merge_or_upload_documents(documents=documents)

        self._initialized = True
        LOGGER.info(
            "Azure AI Search tool index '%s' created with %d tools and %d skills",
            self.index_name,
            len(self._tools),
            len(self._skills),
        )

    async def search(self, query: str, top: int = 5, category: SearchCategory = "all") -> list[ToolSearchResult]:
        """Search the ephemeral Azure AI Search tool index.

        Supports filtering by ``category`` using Azure's OData filter on the
        ``type`` field.
        """
        if top <= 0:
            return []
        if not self._initialized:
            await self.initialize()
        if self._search_client is None:
            return []

        search_text = query.strip() or "*"

        # Build OData filter for category
        filter_expr: str | None = None
        if category == "tools":
            filter_expr = "type eq 'tool'"
        elif category == "skills":
            filter_expr = "type eq 'skill'"

        if query.strip():
            try:
                query_vector = await self._embed_text(query)
                results = await self._search_client.search(
                    search_text=search_text,
                    semantic_query=query,
                    query_type="semantic",
                    semantic_configuration_name=_SEMANTIC_CONFIGURATION_NAME,
                    select=_SELECT_FIELDS,
                    filter=filter_expr,
                    top=top,
                    vector_queries=[  # type: ignore[arg-type]
                        VectorizedQuery(
                            vector=query_vector,
                            fields="description_vector",
                            k_nearest_neighbors=top,
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
            filter=filter_expr,
            top=top,
        )
        return await self._collect_results(results)

    async def close(self) -> None:
        """Delete the ephemeral index and close associated network clients."""
        self._unregister_atexit_cleanup()
        if self._index_client is not None and self._index_created:
            try:
                await self._index_client.delete_index(self.index_name)
            except ResourceNotFoundError:
                pass  # Index already deleted — nothing to clean up.
            except Exception:
                LOGGER.warning("Failed to delete Azure AI Search index '%s' during shutdown", self.index_name)
            self._index_created = False
        await self._close_resource(self._search_client)
        await self._close_resource(self._index_client)
        await self._close_resource(self._http_client)
        await self._close_resource(self._credential)
        self._search_client = None
        self._index_client = None
        self._http_client = None
        self._initialized = False

    async def cleanup(self) -> None:
        """Alias for :meth:`close` to make lifecycle management explicit."""
        await self.close()

    def _register_atexit_cleanup(self) -> None:
        """Register best-effort interpreter-shutdown cleanup for the ephemeral index."""
        if self._atexit_cleanup is not None or not self.endpoint:
            return
        self._atexit_cleanup = functools.partial(
            _delete_index_at_exit,
            endpoint=self.endpoint,
            index_name=self.index_name,
        )
        atexit.register(self._atexit_cleanup)

    def _unregister_atexit_cleanup(self) -> None:
        """Unregister the interpreter-shutdown cleanup hook if present."""
        if self._atexit_cleanup is None:
            return
        atexit.unregister(self._atexit_cleanup)
        self._atexit_cleanup = None

    def _build_index(self, embedding_dimensions: int) -> SearchIndex:
        """Construct the Azure AI Search index schema."""
        fields = [
            SimpleField(name="id", type=SearchFieldDataType.String, key=True, filterable=True),  # type: ignore[arg-type]
            SearchableField(name="name", sortable=True),
            SearchableField(name="server_name", filterable=True, sortable=True),
            SearchableField(name="description"),
            SearchableField(name="affordances", collection=True),
            SearchableField(name="state_requires", collection=True, filterable=True),
            SearchableField(name="state_produces", collection=True, filterable=True),
            SimpleField(name="type", type=SearchFieldDataType.String, filterable=True),  # type: ignore[arg-type]
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
        """Embed and serialize tool and skill metadata for indexing."""
        documents: list[dict[str, Any]] = []

        if self._tools:
            tool_embeddings = await asyncio.gather(
                *(self._embed_text(_tool_embedding_text(tool)) for tool in self._tools)
            )
            for tool, embedding in zip(self._tools, tool_embeddings, strict=True):
                documents.append(
                    {
                        "id": _tool_document_id(tool),
                        "name": tool.name,
                        "server_name": tool.server_name,
                        "description": tool.description,
                        "affordances": list(tool.affordances),
                        "state_requires": list(tool.state_requires),
                        "state_produces": list(tool.state_produces),
                        "type": "tool",
                        "description_vector": embedding,
                    }
                )

        if self._skills:
            skill_embeddings = await asyncio.gather(
                *(self._embed_text(_skill_embedding_text(skill)) for skill in self._skills)
            )
            for skill, embedding in zip(self._skills, skill_embeddings, strict=True):
                documents.append(
                    {
                        "id": _skill_document_id(skill),
                        "name": skill.get("name", ""),
                        "server_name": self.server_name,
                        "description": skill.get("description", ""),
                        "affordances": skill.get("states", []),
                        "state_requires": [],
                        "state_produces": [],
                        "type": "skill",
                        "description_vector": embedding,
                    }
                )

        return documents

    async def _embed_text(self, text: str) -> list[float]:
        """Generate an embedding for *text* using Azure OpenAI over HTTP."""
        deployment = os.getenv(TOOL_SEARCH_VECTORIZER_DEPLOYMENT_ENV)
        if not deployment:
            raise ValueError(f"Embedding deployment is required; set {TOOL_SEARCH_VECTORIZER_DEPLOYMENT_ENV}.")

        client = await self._get_http_client()
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

    async def _get_http_client(self) -> httpx.AsyncClient:
        """Create or return the shared HTTP client used for embeddings.

        Uses double-checked locking to avoid creating duplicate clients
        when multiple embedding tasks run concurrently.
        """
        if self._http_client is None:
            async with self._http_client_lock:
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
            doc_type = str(document.get("type", "tool"))
            name = str(document.get("name", ""))

            if doc_type == "skill":
                to_access = f'Load with load_{self.server_name}_skill(skill_name="{name}")'
                execution_type = "skill"
            else:
                to_access = f"Call via execute_{self.server_name}_code"
                execution_type = "mcp"

            output.append(
                ToolSearchResult(
                    name=name,
                    server_name=str(document.get("server_name", "")),
                    description=str(document.get("description", "")),
                    execution_type=execution_type,
                    type=doc_type,
                    to_access=to_access,
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
