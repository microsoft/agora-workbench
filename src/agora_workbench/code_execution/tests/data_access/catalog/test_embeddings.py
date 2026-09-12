"""Tests for optional catalog embedding providers."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ....data_access.catalog import embeddings


def test_keyword_models_do_not_probe_optional_dependencies(monkeypatch):
    monkeypatch.setattr(
        embeddings,
        "find_spec",
        lambda _name: pytest.fail("keyword-only provider should not probe OpenAI"),
    )

    assert embeddings.create_embedding_provider("none") is None
    assert embeddings.create_embedding_provider("keyword") is None


def test_azure_openai_reports_missing_client_extra(monkeypatch):
    monkeypatch.setattr(embeddings, "find_spec", lambda _name: None)

    with pytest.raises(RuntimeError, match=r"agora-workbench\[catalog-vector\]"):
        embeddings.create_embedding_provider(
            "azure-openai",
            azure_openai_endpoint="https://example.openai.azure.com",
            azure_openai_deployment="embedding",
            credential_provider=object(),  # type: ignore[arg-type]
        )


def test_unsupported_embedding_model_is_actionable():
    with pytest.raises(ValueError, match="Use 'none'.*or 'azure-openai'"):
        embeddings.create_embedding_provider("local-model")


def test_embedding_dimensions_must_be_positive(monkeypatch):
    monkeypatch.setattr(embeddings, "find_spec", lambda _name: object())

    with pytest.raises(ValueError, match="greater than zero"):
        embeddings.create_embedding_provider(
            "azure-openai",
            azure_openai_endpoint="https://example.openai.azure.com",
            azure_openai_deployment="embedding",
            credential_provider=object(),  # type: ignore[arg-type]
            dimensions=0,
        )


@pytest.mark.asyncio
async def test_service_default_dimensions_are_not_sent(monkeypatch):
    response = SimpleNamespace(data=[SimpleNamespace(embedding=[0.1, 0.2, 0.3])])
    create = AsyncMock(return_value=response)
    client = SimpleNamespace(embeddings=SimpleNamespace(create=create), close=AsyncMock())

    def client_factory(**kwargs):
        del kwargs
        return client

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(AsyncAzureOpenAI=client_factory))

    provider = embeddings.AzureOpenAIEmbeddingProvider(
        endpoint="https://example.openai.azure.com",
        deployment="embedding",
        credential_provider=object(),  # type: ignore[arg-type]
    )

    result = await provider.embed(["text"])

    assert result == [[0.1, 0.2, 0.3]]
    assert provider.dimensions == 3
    assert create.await_args.kwargs == {"input": ["text"], "model": "embedding"}


@pytest.mark.asyncio
async def test_explicit_dimensions_use_supported_api_version(monkeypatch):
    response = SimpleNamespace(data=[SimpleNamespace(embedding=[0.1, 0.2])])
    create = AsyncMock(return_value=response)
    client = SimpleNamespace(embeddings=SimpleNamespace(create=create), close=AsyncMock())
    client_kwargs = {}

    def client_factory(**kwargs):
        client_kwargs.update(kwargs)
        return client

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(AsyncAzureOpenAI=client_factory))

    provider = embeddings.AzureOpenAIEmbeddingProvider(
        endpoint="https://example.openai.azure.com",
        deployment="embedding",
        credential_provider=object(),  # type: ignore[arg-type]
        dimensions=2,
    )

    await provider.embed(["text"])

    assert client_kwargs["api_version"] == "2024-02-01"
    assert create.await_args.kwargs == {
        "input": ["text"],
        "model": "embedding",
        "dimensions": 2,
    }


@pytest.mark.asyncio
async def test_explicit_dimensions_validate_service_response(monkeypatch):
    response = SimpleNamespace(data=[SimpleNamespace(embedding=[0.1, 0.2, 0.3])])
    client = SimpleNamespace(
        embeddings=SimpleNamespace(create=AsyncMock(return_value=response)),
        close=AsyncMock(),
    )

    def client_factory(**kwargs):
        del kwargs
        return client

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(AsyncAzureOpenAI=client_factory))
    provider = embeddings.AzureOpenAIEmbeddingProvider(
        endpoint="https://example.openai.azure.com",
        deployment="embedding",
        credential_provider=object(),  # type: ignore[arg-type]
        dimensions=2,
    )

    with pytest.raises(ValueError, match="requested 2, received 3"):
        await provider.embed(["text"])


@pytest.mark.asyncio
async def test_close_releases_owned_credential():
    credential = SimpleNamespace(close=AsyncMock())
    provider = embeddings.AzureOpenAIEmbeddingProvider(
        endpoint="https://example.openai.azure.com",
        deployment="embedding",
        credential_provider=credential,  # type: ignore[arg-type]
        credential_owned=True,
    )

    await provider.close()

    credential.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_close_supports_owned_sync_credential():
    credential = SimpleNamespace(close=lambda: None)
    provider = embeddings.AzureOpenAIEmbeddingProvider(
        endpoint="https://example.openai.azure.com",
        deployment="embedding",
        credential_provider=credential,  # type: ignore[arg-type]
        credential_owned=True,
    )

    await provider.close()

    assert not provider._credential_owned
