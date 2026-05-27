"""BYO-LLM factory for the MAF quickstart tutorial.

A thin wrapper around the project-wide LLM abstraction in ``agent_helpers/llm/``:
maps the tutorial's ``LLM_PROVIDER`` env var onto a :class:`agent_helpers.llm.ModelSpec`
and hands it to :func:`agent_helpers.llm.factories.make_maf_client`.

Supported ``$LLM_PROVIDER`` values:
    azure_openai_entra  - Azure OpenAI via Entra ID token (default)
    azure_openai_apikey - Azure OpenAI via API key
    openai              - OpenAI (api.openai.com or compatible)
    ollama              - Ollama local server (OpenAI-compatible endpoint)

Contract: any object satisfying agent_framework's ChatClient protocol works.
The factory pattern is just a convenience; users can replace this entirely
with their own client construction.
"""

from __future__ import annotations

import logging
import os

from agent_helpers.llm import ModelSpec
from agent_helpers.llm.factories import make_maf_client

LOGGER = logging.getLogger(__name__)


def build_chat_client():
    """Build a MAF ChatClient based on $LLM_PROVIDER.

    Returns:
        An ``agent_framework`` chat client instance.

    Raises:
        ValueError: If required environment variables for the chosen provider
            are missing, or if `$LLM_PROVIDER` is unrecognized.
    """
    provider = os.getenv("LLM_PROVIDER", "azure_openai_entra").lower()
    LOGGER.info("Building chat client for LLM_PROVIDER=%s", provider)

    if provider == "azure_openai_entra":
        return _build_azure_openai_entra()
    if provider == "azure_openai_apikey":
        return _build_azure_openai_apikey()
    if provider == "openai":
        return _build_openai()
    if provider == "ollama":
        return _build_ollama()

    raise ValueError(
        f"Unknown LLM_PROVIDER={provider!r}. Expected one of: "
        "azure_openai_entra, azure_openai_apikey, openai, ollama."
    )


def _build_azure_openai_entra():
    """Azure OpenAI via Entra ID.

    Reads: ``AZURE_OPENAI_ENDPOINT``, ``AOAI_SCOPE``, ``API_VERSION``,
    ``AZURE_OPENAI_DEPLOYMENT_NAME`` (preferred) or ``MODEL_DEPLOYMENT_NAME``.

    ``auth_mode="entra"`` ensures we use the Entra credential factory even
    if a stale ``AZURE_OPENAI_API_KEY`` is sitting in the environment — the
    user explicitly chose ``LLM_PROVIDER=azure_openai_entra``.
    """
    spec = ModelSpec.from_env(auth_mode="entra")
    return make_maf_client(spec)


def _build_azure_openai_apikey():
    """Azure OpenAI via API key (no Entra).

    Reads: ``AZURE_OPENAI_ENDPOINT``, ``AZURE_OPENAI_API_KEY``,
    ``API_VERSION``, and the deployment name.
    """
    spec = ModelSpec.from_env(auth_mode="api_key")
    return make_maf_client(spec)


def _build_openai():
    """OpenAI (api.openai.com or any OpenAI-compatible endpoint)."""
    spec = ModelSpec.from_env(provider="openai")
    return make_maf_client(spec)


def _build_ollama():
    """Ollama via its OpenAI-compatible endpoint."""
    spec = ModelSpec.from_env(provider="ollama")
    return make_maf_client(spec)
