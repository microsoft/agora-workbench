"""Shared fixtures for the LLM abstraction tests."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Callable

import pytest

# Every env var ModelSpec.from_env reads, across all four providers.
_ENV_VARS = (
    # Azure OpenAI
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_DEPLOYMENT_NAME",
    "AZURE_OPENAI_API_KEY",
    "MODEL_DEPLOYMENT_NAME",
    "API_VERSION",
    "AOAI_SCOPE",
    # OpenAI
    "OPENAI_API_KEY",
    "OPENAI_MODEL",
    "OPENAI_BASE_URL",
    # Ollama
    "OLLAMA_BASE_URL",
    "OLLAMA_MODEL",
    # LiteLLM
    "LITELLM_BASE_URL",
    "LITELLM_API_KEY",
    "LITELLM_MODEL",
    # Inference defaults
    "LLM_TEMPERATURE",
    "LLM_MAX_TOKENS",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    """Remove every env var ModelSpec.from_env reads.

    Tests that exercise from_env should set only the env vars they care about,
    starting from this clean baseline.
    """
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    yield monkeypatch


@pytest.fixture
def fake_credential_factory() -> Callable[[], Callable[[], str]]:
    """Return a stub credential_factory suitable for ModelSpec construction.

    MAF requires the credential_factory's return value to be either an Azure
    TokenCredential or a callable token provider. We use the callable form
    since it doesn't require importing azure-identity.
    """
    return lambda: (lambda: "fake-bearer-token")
