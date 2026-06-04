"""Pluggable embedding providers for the catalog."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ...auth import CredentialProvider

LOGGER = logging.getLogger(__name__)


class EmbeddingProvider(Protocol):
    """Protocol for embedding computation."""

    @property
    def dimensions(self) -> int:
        """Dimensionality of the output vectors."""
        ...

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Compute embeddings for a batch of texts."""
        ...


class AzureOpenAIEmbeddingProvider(EmbeddingProvider):
    """Embedding provider using Azure OpenAI.

    Accepts a ``CredentialProvider`` from the auth module for token
    acquisition, consistent with the rest of the codebase.
    """

    _SCOPE = "https://cognitiveservices.azure.com/.default"

    def __init__(
        self,
        endpoint: str,
        deployment: str,
        credential_provider: CredentialProvider,
        dimensions: int = 3072,
    ):
        self._endpoint = endpoint
        self._deployment = deployment
        self._credential_provider = credential_provider
        self._dimensions = dimensions
        self._client = None

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def _ensure_client(self):
        """Lazy-initialize the OpenAI client (once)."""
        if self._client is None:
            from openai import AsyncAzureOpenAI

            self._client = AsyncAzureOpenAI(
                azure_endpoint=self._endpoint,
                azure_ad_token_provider=self._get_token,
                api_version="2023-05-15",
            )

    async def _get_token(self) -> str:
        """Token provider callback for the Azure OpenAI client."""
        token = await self._credential_provider.get_token(self._SCOPE)
        return token.token

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self._ensure_client()

        response = await self._client.embeddings.create(
            input=texts,
            model=self._deployment,
        )

        return [item.embedding for item in response.data]

    async def close(self) -> None:
        """Release client resources."""
        if self._client is not None:
            await self._client.close()
            self._client = None


def create_embedding_provider(
    model_name: str,
    azure_openai_endpoint: str | None = None,
    azure_openai_deployment: str | None = None,
    credential_provider: CredentialProvider | None = None,
) -> EmbeddingProvider | None:
    """Factory to create an embedding provider from config.

    Returns ``None`` for a keyword-only (SQLite FTS5 / BM25) catalog — when
    ``model_name`` is ``"none"`` or empty. Returns an Azure OpenAI provider for
    ``"azure-openai"``. The indexer and search skip vector embedding when this
    is ``None``.
    """
    if not model_name or model_name.lower() in ("none", "bm25", "keyword"):
        return None
    if model_name != "azure-openai":
        raise ValueError(
            f"Unsupported embedding model '{model_name}'. Use 'none' (keyword/BM25 only) or 'azure-openai'."
        )
    if not azure_openai_endpoint or not azure_openai_deployment:
        raise ValueError(
            "azure_openai_endpoint and azure_openai_deployment are required when embedding_model is 'azure-openai'"
        )
    if credential_provider is None:
        from ...auth import EntraCredentialProvider

        credential_provider = EntraCredentialProvider()
    return AzureOpenAIEmbeddingProvider(
        endpoint=azure_openai_endpoint,
        deployment=azure_openai_deployment,
        credential_provider=credential_provider,
    )
