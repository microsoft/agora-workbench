"""Pluggable embedding providers for the catalog."""

from __future__ import annotations

import logging
from importlib.util import find_spec
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ...auth import CredentialProvider

LOGGER = logging.getLogger(__name__)

_AZURE_OPENAI_API_VERSION = "2024-02-01"


class EmbeddingProvider(Protocol):
    """Protocol for embedding computation."""

    @property
    def dimensions(self) -> int | None:
        """Configured or observed dimensionality of the output vectors."""
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
        dimensions: int | None = None,
    ):
        self._endpoint = endpoint
        self._deployment = deployment
        self._credential_provider = credential_provider
        self._dimensions = dimensions
        self._client = None

    @property
    def dimensions(self) -> int | None:
        return self._dimensions

    def _ensure_client(self):
        """Lazy-initialize the OpenAI client (once)."""
        if self._client is None:
            try:
                from openai import AsyncAzureOpenAI
            except ImportError as exc:
                raise RuntimeError(
                    "Azure OpenAI catalog embeddings require the 'agora-workbench[catalog-vector]' extra."
                ) from exc

            self._client = AsyncAzureOpenAI(
                azure_endpoint=self._endpoint,
                azure_ad_token_provider=self._get_token,
                api_version=_AZURE_OPENAI_API_VERSION,
            )

    async def _get_token(self) -> str:
        """Token provider callback for the Azure OpenAI client."""
        token = await self._credential_provider.get_token(self._SCOPE)
        return token.token

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self._ensure_client()

        request = {
            "input": texts,
            "model": self._deployment,
        }
        if self._dimensions is not None:
            request["dimensions"] = self._dimensions
        response = await self._client.embeddings.create(**request)

        embeddings = [item.embedding for item in response.data]
        if not embeddings:
            return embeddings

        observed_dimensions = len(embeddings[0])
        if self._dimensions is None:
            self._dimensions = observed_dimensions
        for embedding in embeddings:
            if len(embedding) != observed_dimensions:
                raise ValueError("Azure OpenAI returned embeddings with inconsistent dimensions.")
            if self._dimensions is not None and len(embedding) != self._dimensions:
                raise ValueError(
                    "Azure OpenAI embedding dimension mismatch: "
                    f"requested {self._dimensions}, received {len(embedding)}."
                )
        return embeddings

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
    dimensions: int | None = None,
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
    if dimensions is not None and dimensions <= 0:
        raise ValueError("Embedding dimensions must be greater than zero.")
    if find_spec("openai") is None:
        raise RuntimeError("Azure OpenAI catalog embeddings require the 'agora-workbench[catalog-vector]' extra.")
    if credential_provider is None:
        try:
            from ...auth import EntraCredentialProvider
        except ImportError as exc:
            raise RuntimeError(
                "Azure OpenAI catalog embeddings require Azure credentials. "
                "Install the 'agora-workbench[azure,catalog-vector]' extras."
            ) from exc

        try:
            credential_provider = EntraCredentialProvider()
        except ImportError as exc:
            raise RuntimeError(
                "Azure OpenAI catalog embeddings require Azure credentials. "
                "Install the 'agora-workbench[azure,catalog-vector]' extras."
            ) from exc
    return AzureOpenAIEmbeddingProvider(
        endpoint=azure_openai_endpoint,
        deployment=azure_openai_deployment,
        credential_provider=credential_provider,
        dimensions=dimensions,
    )
