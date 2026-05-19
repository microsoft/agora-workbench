"""Pluggable embedding providers for the catalog."""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

LOGGER = logging.getLogger(__name__)


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Protocol for embedding computation."""

    @property
    def dimensions(self) -> int:
        """Dimensionality of the output vectors."""
        ...

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Compute embeddings for a batch of texts."""
        ...


class LocalEmbeddingProvider:
    """Embedding provider using sentence-transformers on CPU."""

    def __init__(self, model_name: str = "nomic-ai/nomic-embed-text-v1.5"):
        self._model_name = model_name
        self._model = None

    def _load_model(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            LOGGER.info("Loading embedding model: %s", self._model_name)
            self._model = SentenceTransformer(self._model_name, trust_remote_code=True)
            LOGGER.info("Embedding model loaded (dimensions=%d)", self._model.get_sentence_embedding_dimension())

    @property
    def dimensions(self) -> int:
        self._load_model()
        return self._model.get_sentence_embedding_dimension()  # type: ignore[union-attr]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self._load_model()
        # sentence-transformers encode is synchronous; run in thread for async compat
        import asyncio

        loop = asyncio.get_event_loop()
        embeddings = await loop.run_in_executor(
            None,
            lambda: self._model.encode(texts, normalize_embeddings=True).tolist(),  # type: ignore[union-attr]
        )
        return embeddings


class AzureOpenAIEmbeddingProvider:
    """Embedding provider using Azure OpenAI."""

    def __init__(self, endpoint: str, deployment: str, dimensions: int = 3072):
        self._endpoint = endpoint
        self._deployment = deployment
        self._dimensions = dimensions

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        from openai import AsyncAzureOpenAI
        from azure.identity.aio import DefaultAzureCredential

        credential = DefaultAzureCredential()
        token = await credential.get_token("https://cognitiveservices.azure.com/.default")

        client = AsyncAzureOpenAI(
            azure_endpoint=self._endpoint,
            azure_ad_token=token.token,
            api_version="2023-05-15",
        )

        response = await client.embeddings.create(
            input=texts,
            model=self._deployment,
        )

        await credential.close()
        await client.close()

        return [item.embedding for item in response.data]


def create_embedding_provider(
    model_name: str,
    azure_openai_endpoint: str | None = None,
    azure_openai_deployment: str | None = None,
) -> EmbeddingProvider:
    """Factory to create the appropriate embedding provider from config."""
    if model_name == "azure-openai":
        if not azure_openai_endpoint or not azure_openai_deployment:
            raise ValueError(
                "azure_openai_endpoint and azure_openai_deployment are required when embedding_model is 'azure-openai'"
            )
        return AzureOpenAIEmbeddingProvider(
            endpoint=azure_openai_endpoint,
            deployment=azure_openai_deployment,
        )
    return LocalEmbeddingProvider(model_name=model_name)
