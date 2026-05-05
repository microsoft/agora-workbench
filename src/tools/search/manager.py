"""
Lifecycle management for the Azure AI Search tool index.

Deploy, populate, and clean up the tool index when a run starts and ends..

Environment variables:
    TOOL_SEARCH_ENDPOINT: Required - Azure AI Search service endpoint
    TOOL_SEARCH_VECTORIZER_ENDPOINT: Required - Azure OpenAI endpoint for embeddings
    TOOL_SEARCH_VECTORIZER_DEPLOYMENT: Required - embedding model deployment name
"""

import asyncio
import atexit
import json
import logging
import os
import signal
import sys
from pathlib import Path
from uuid import uuid4

import httpx
from jinja2 import Template

from auth import get_search_credential, get_search_credential_async
from tools.search.build_tool_list import ToolInfo
from tools.search._constants import (
    TOOL_SEARCH_ENDPOINT_ENV,
    TOOL_SEARCH_VECTORIZER_ENDPOINT_ENV,
    TOOL_SEARCH_VECTORIZER_DEPLOYMENT_ENV,
    _OPENAI_EMBEDDING_API_VERSION,
)

LOGGER = logging.getLogger(__name__)

INDEX_NAME_PREFIX = "tool-registry"
_SEARCH_API_VERSION = "2025-11-01-preview"


class ToolSearchIndexManager:
    """
    Manages the lifecycle of the Azure AI Search tool index.

    Deploy and populate the index at run start; clean up at run end.
    Cleanup is registered with ``atexit`` and signal handlers (SIGTERM/SIGINT)
    so the index is deleted even when the process exits abnormally.

    Usage::

        manager = ToolSearchIndexManager.from_env()
        await manager.setup(tool_list)  # deploy + populate + register cleanup

        # --- agent run ---

        await manager.delete_index()  # explicit cleanup

    Or as an async context manager::

        async with ToolSearchIndexManager.from_env() as manager:
            await manager.populate_index(tool_list)
            # --- agent run ---
    """

    def __init__(
        self,
        search_endpoint: str,
        azure_openai_endpoint: str,
        azure_openai_embedding_deployment: str,
    ):
        """
        Initialize the manager.

        A unique index name is generated automatically so that each agent
        run operates on its own isolated index (``tool-registry-<hex8>``).

        Args:
            search_endpoint: Azure AI Search service endpoint
                             (e.g. ``https://my-service.search.windows.net``)
            azure_openai_endpoint: Azure OpenAI endpoint for generating
                                   embeddings and for the integrated vectorizer
                                   used in hybrid (keyword + vector) retrieval.
            azure_openai_embedding_deployment: Deployment name for the
                                               embedding model (e.g.
                                               ``text-embedding-3-large``).
        """
        self.search_endpoint = search_endpoint.rstrip("/")
        self.index_name = f"{INDEX_NAME_PREFIX}-{uuid4().hex[:8]}"
        self.azure_openai_endpoint = azure_openai_endpoint.rstrip("/")
        self.azure_openai_embedding_deployment = azure_openai_embedding_deployment
        self._index_deployed = False
        self._cleanup_registered = False
        self._async_credential = None

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls) -> "ToolSearchIndexManager":
        """
        Create a manager from environment variables.

        A unique index name is generated automatically so each agent
        run creates and manages its own ephemeral index.

        Raises:
            ValueError: If any required environment variable is not set.
        """
        missing = []
        endpoint = os.getenv(TOOL_SEARCH_ENDPOINT_ENV)
        if not endpoint:
            missing.append(TOOL_SEARCH_ENDPOINT_ENV)

        azure_openai_endpoint = os.getenv(TOOL_SEARCH_VECTORIZER_ENDPOINT_ENV)
        if not azure_openai_endpoint:
            missing.append(TOOL_SEARCH_VECTORIZER_ENDPOINT_ENV)

        azure_openai_embedding_deployment = os.getenv(TOOL_SEARCH_VECTORIZER_DEPLOYMENT_ENV)
        if not azure_openai_embedding_deployment:
            missing.append(TOOL_SEARCH_VECTORIZER_DEPLOYMENT_ENV)

        if missing:
            raise ValueError(
                f"Tool search not configured. Set the following environment variable(s): {', '.join(missing)}"
            )

        # Narrow types for mypy after the missing-check guarantees non-None
        assert endpoint
        assert azure_openai_endpoint
        assert azure_openai_embedding_deployment

        return cls(
            search_endpoint=endpoint,
            azure_openai_endpoint=azure_openai_endpoint,
            azure_openai_embedding_deployment=azure_openai_embedding_deployment,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_credential_async(self):
        """Return the cached async credential, creating it on first use."""
        if self._async_credential is None:
            self._async_credential = get_search_credential_async()
        return self._async_credential

    async def _close_credential_async(self) -> None:
        """Close and discard the cached async credential (no-op for API key)."""
        if self._async_credential is not None:
            try:
                if hasattr(self._async_credential, "close"):
                    await self._async_credential.close()
            except Exception:
                LOGGER.debug("Error closing async credential", exc_info=True)
            self._async_credential = None

    async def _get_auth_headers(self) -> dict[str, str]:
        """Get authentication headers for raw HTTP calls to Azure AI Search.

        Returns api-key header for key-based auth, Bearer token for Entra.
        """
        from azure.core.credentials import AzureKeyCredential

        credential = self._get_credential_async()
        if isinstance(credential, AzureKeyCredential):
            return {"api-key": credential.key}
        token_response = await credential.get_token("https://search.azure.com/.default")
        return {"Authorization": f"Bearer {token_response.token}"}

    def _render_index_definition(self) -> dict:
        """Render the Jinja index template with the current index name.

        The rendered definition includes a ``description_vector`` field,
        an HNSW algorithm, and an Azure OpenAI integrated vectorizer for
        hybrid (keyword + vector + semantic reranker) search.
        """
        template_path = Path(__file__).parent / "index.jinja"
        with open(template_path) as f:
            template_str = f.read()
        rendered = Template(template_str).render(
            index_name=self.index_name,
            azure_openai_endpoint=self.azure_openai_endpoint,
            azure_openai_embedding_deployment=self.azure_openai_embedding_deployment,
        )
        return json.loads(rendered)

    def _build_documents(self, tools: list[ToolInfo]) -> list[dict]:
        """Build Azure Search documents from a list of ToolInfo objects."""
        documents = []
        for tool_info in tools:
            server_name = tool_info.server_name
            tool_id = f"{server_name}--{tool_info.name}" if server_name else tool_info.name
            doc: dict = {
                "tool_id": tool_id,
                "name": tool_info.name,
                "description": tool_info.description,
                "server_name": server_name,
                "affordances": " ".join(tool_info.affordances),
                "state_requires": list(tool_info.state_requires),
                "state_produces": list(tool_info.state_produces),
            }
            documents.append(doc)
        return documents

    async def _add_embeddings(self, documents: list[dict]) -> None:
        """
        Generate embeddings for tool metadata and add them to documents.

        Calls the Azure OpenAI embeddings API to vectorise
        ``name + " " + description + " " + affordances`` for each
        document, then stores the result in the ``description_vector``
        field.
        """
        if not documents:
            return

        texts = [f"{doc['name']} {doc['description']} {doc.get('affordances', '')}".strip() for doc in documents]

        from auth import get_token_provider

        token_provider = get_token_provider("https://cognitiveservices.azure.com/.default")

        url = f"{self.azure_openai_endpoint}/openai/deployments/{self.azure_openai_embedding_deployment}/embeddings"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token_provider()}",
        }
        params = {"api-version": _OPENAI_EMBEDDING_API_VERSION}

        LOGGER.info(
            "Generating embeddings for %d tool descriptions via %s",
            len(texts),
            self.azure_openai_embedding_deployment,
        )

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                url,
                json={"input": texts},
                headers=headers,
                params=params,
            )

        if response.status_code != 200:
            LOGGER.error(
                "Embedding generation failed: HTTP %s %s",
                response.status_code,
                response.text,
            )
            response.raise_for_status()

        data = response.json()["data"]
        # The API returns items in order, but sort by index to be safe
        data.sort(key=lambda item: item["index"])

        for doc, emb in zip(documents, data):
            doc["description_vector"] = emb["embedding"]

        LOGGER.info("Generated embeddings for %d tool descriptions", len(documents))

    # ------------------------------------------------------------------
    # Public lifecycle methods
    # ------------------------------------------------------------------

    async def deploy_index(self) -> None:
        """
        Create or update the tool search index.

        Renders ``index.jinja``, then PUTs the definition to Azure AI Search.

        Raises:
            httpx.HTTPStatusError: If the index creation request fails.
        """
        index_payload = self._render_index_definition()
        auth_headers = await self._get_auth_headers()

        url = f"{self.search_endpoint}/indexes/{self.index_name}"
        headers = {
            "Content-Type": "application/json",
            **auth_headers,
        }
        params = {"api-version": _SEARCH_API_VERSION}

        LOGGER.info("Deploying tool search index: %s", self.index_name)
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.put(url, json=index_payload, headers=headers, params=params)

        if response.status_code not in (200, 201, 204):
            LOGGER.error(
                "Failed to deploy tool search index '%s': HTTP %s %s",
                self.index_name,
                response.status_code,
                response.text,
            )
            response.raise_for_status()

        LOGGER.info("Tool search index deployed: %s", self.index_name)
        self._index_deployed = True

    async def populate_index(self, tools: list[ToolInfo]) -> None:
        """
        Upload tool documents to the search index.

        Args:
            tools: List of ToolInfo objects discovered from MCP servers.

        Raises:
            httpx.HTTPStatusError: If the document upload request fails.
        """
        if not self._index_deployed:
            LOGGER.warning("Index not deployed yet; skipping population")
            return

        documents = self._build_documents(tools)
        if not documents:
            LOGGER.warning("No tools found; index will be empty")
            return

        # Generate embeddings for vector search
        await self._add_embeddings(documents)

        # Wrap each document with the upload action for the batch index API
        batch = {"value": [{"@search.action": "upload", **doc} for doc in documents]}

        auth_headers = await self._get_auth_headers()
        url = f"{self.search_endpoint}/indexes/{self.index_name}/docs/index"
        headers = {
            "Content-Type": "application/json",
            **auth_headers,
        }
        params = {"api-version": _SEARCH_API_VERSION}

        LOGGER.info("Populating tool search index with %d tools", len(documents))
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, json=batch, headers=headers, params=params)

        if response.status_code not in (200, 207):
            LOGGER.error(
                "Failed to upload documents to tool search index: HTTP %s %s",
                response.status_code,
                response.text,
            )
            response.raise_for_status()

        result = response.json()
        succeeded = sum(1 for item in result.get("value", []) if item.get("status"))
        LOGGER.info("Indexed %d/%d tools", succeeded, len(documents))

        # Azure AI Search indexes may not be immediately queryable after
        # document upload.  A brief pause avoids "no results" on the first query.
        await asyncio.sleep(3)

    async def delete_index(self) -> None:
        """
        Delete the tool search index.

        No-op if the index was never deployed in this session.
        Errors are logged but not re-raised to avoid masking the original
        exception during cleanup.
        """
        if not self._index_deployed:
            return

        try:
            auth_headers = await self._get_auth_headers()
            url = f"{self.search_endpoint}/indexes/{self.index_name}"
            headers = {**auth_headers}
            params = {"api-version": _SEARCH_API_VERSION}

            LOGGER.info("Deleting tool search index: %s", self.index_name)
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.delete(url, headers=headers, params=params)

            if response.status_code not in (200, 204, 404):
                LOGGER.warning(
                    "Unexpected status deleting tool search index: HTTP %s",
                    response.status_code,
                )
            else:
                LOGGER.info("Tool search index deleted: %s", self.index_name)

            self._index_deployed = False
        except Exception as exc:
            LOGGER.error("Error deleting tool search index '%s': %s", self.index_name, exc)
        finally:
            await self._close_credential_async()

    async def setup(self, tools: list[ToolInfo]) -> None:
        """
        Deploy the index, populate it, and register cleanup handlers.

        This is the primary entry point for run-start lifecycle management.

        Args:
            tools: List of ToolInfo objects discovered from MCP servers.
        """
        await self.deploy_index()
        await self.populate_index(tools)
        self._register_cleanup()

    # ------------------------------------------------------------------
    # Cleanup handlers
    # ------------------------------------------------------------------

    def _sync_cleanup(self) -> None:
        """
        Synchronous cleanup suitable for atexit and signal handlers.

        Uses a synchronous HTTP client and sync credential to avoid relying
        on the asyncio event loop, which may already be torn down during
        interpreter shutdown.
        """
        if not self._index_deployed:
            return

        LOGGER.debug("Running tool search index cleanup")
        try:
            from azure.core.credentials import AzureKeyCredential

            credential = get_search_credential()
            if isinstance(credential, AzureKeyCredential):
                headers = {"api-key": credential.key}
            else:
                token_response = credential.get_token("https://search.azure.com/.default")
                headers = {"Authorization": f"Bearer {token_response.token}"}
            url = f"{self.search_endpoint}/indexes/{self.index_name}"
            params = {"api-version": _SEARCH_API_VERSION}

            LOGGER.info("Deleting tool search index: %s", self.index_name)
            with httpx.Client(timeout=30.0) as client:
                response = client.delete(url, headers=headers, params=params)

            if response.status_code not in (200, 204, 404):
                LOGGER.warning(
                    "Unexpected status deleting tool search index: HTTP %s",
                    response.status_code,
                )
            else:
                LOGGER.info("Tool search index deleted: %s", self.index_name)
                self._index_deployed = False
        except Exception as exc:
            LOGGER.error("Error during tool search index cleanup: %s", exc)

    def _register_cleanup(self) -> None:
        """
        Register cleanup handlers for normal and abnormal process exits.

        Registers:
        - ``atexit`` handler for normal Python interpreter shutdown.
        - SIGTERM handler for graceful container/process termination.
        - SIGINT handler for keyboard interrupt (Ctrl-C).

        Existing signal handlers are chained so they are still invoked.
        Only registers once per instance even if called multiple times.
        """
        if self._cleanup_registered:
            return

        atexit.register(self._sync_cleanup)

        # Chain signal handlers so existing handlers are still called
        _original_sigterm = signal.getsignal(signal.SIGTERM)
        _original_sigint = signal.getsignal(signal.SIGINT)

        def _handle_sigterm(signum, frame):  # type: ignore[misc]
            self._sync_cleanup()
            if callable(_original_sigterm):
                _original_sigterm(signum, frame)
            else:
                sys.exit(0)

        def _handle_sigint(signum, frame):  # type: ignore[misc]
            self._sync_cleanup()
            if callable(_original_sigint):
                _original_sigint(signum, frame)
            else:
                raise KeyboardInterrupt()

        signal.signal(signal.SIGTERM, _handle_sigterm)
        signal.signal(signal.SIGINT, _handle_sigint)

        self._cleanup_registered = True
        LOGGER.debug("Registered tool search index cleanup handlers (atexit + SIGTERM + SIGINT)")

    # ------------------------------------------------------------------
    # Async context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "ToolSearchIndexManager":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.delete_index()
        await self._close_credential_async()
