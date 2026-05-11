"""
BYO-LLM factory for the MAF quickstart tutorial.

`build_chat_client()` returns an `agent_framework` chat client based on the
`LLM_PROVIDER` environment variable. Lazy imports keep optional providers
from being a hard dependency.

Supported providers:
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

    Reads:
        AZURE_OPENAI_ENDPOINT, AOAI_SCOPE, API_VERSION,
        AZURE_OPENAI_DEPLOYMENT_NAME (preferred) or MODEL_DEPLOYMENT_NAME (fallback)
    """
    # In agent_framework >= 1.2, AzureOpenAIChatClient was unified into
    # OpenAIChatClient (which accepts azure_endpoint + api_version +
    # credential for the Azure path).
    from agent_framework.openai import OpenAIChatClient

    # Reuse the repo's central credential factory so this picks up the same
    # AzureCli -> ManagedIdentity chain used everywhere else.
    from auth import get_token_provider

    endpoint = _require("AZURE_OPENAI_ENDPOINT")
    scope = _require("AOAI_SCOPE")
    api_version = _require("API_VERSION")
    deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME") or _require(
        "MODEL_DEPLOYMENT_NAME"
    )

    token_provider = get_token_provider(scope)
    return OpenAIChatClient(
        azure_endpoint=endpoint,
        api_version=api_version,
        model=deployment,
        credential=token_provider,
    )


def _build_azure_openai_apikey():
    """Azure OpenAI via API key (no Entra)."""
    from agent_framework.openai import OpenAIChatClient

    endpoint = _require("AZURE_OPENAI_ENDPOINT")
    api_key = _require("AZURE_OPENAI_API_KEY")
    api_version = _require("API_VERSION")
    deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME") or _require(
        "MODEL_DEPLOYMENT_NAME"
    )

    return OpenAIChatClient(
        azure_endpoint=endpoint,
        api_version=api_version,
        model=deployment,
        api_key=api_key,
    )


def _build_openai():
    """OpenAI (api.openai.com)."""
    from agent_framework.openai import OpenAIChatClient

    api_key = _require("OPENAI_API_KEY")
    model = os.getenv("OPENAI_MODEL", "gpt-4o")
    return OpenAIChatClient(api_key=api_key, model_id=model)


def _build_ollama():
    """Ollama via its OpenAI-compatible endpoint."""
    from agent_framework.openai import OpenAIChatClient

    base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    model = os.getenv("OLLAMA_MODEL", "llama3.1")
    # Ollama doesn't require a real key, but the OpenAI client expects one.
    return OpenAIChatClient(api_key="ollama", base_url=base_url, model_id=model)


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(
            f"Environment variable {name!r} is required for LLM_PROVIDER="
            f"{os.getenv('LLM_PROVIDER', 'azure_openai_entra')}. "
            f"See docs/tutorials/maf_quickstart/.env.example."
        )
    return value
