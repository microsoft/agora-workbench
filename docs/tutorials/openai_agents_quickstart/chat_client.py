"""BYO-LLM factory for the OpenAI Agents quickstart.

Produces a model object that ``agents.Agent(model=...)`` accepts.

Supported providers (set via ``$LLM_PROVIDER``):

  * ``azure_openai_entra`` *(default)* — ``OpenAIResponsesModel`` wrapping
    ``openai.AsyncAzureOpenAI(azure_ad_token_provider=...)``. Set
    ``AZURE_OPENAI_API_KIND=chat_completions`` to use
    ``OpenAIChatCompletionsModel`` instead for gateways that don't expose
    ``/responses``.
  * ``openai`` — returns a bare model id string (``"gpt-4o"`` etc.); the
    openai-agents SDK reads ``OPENAI_API_KEY`` from the env directly.

The MAF quickstart's ``chat_client.py`` shows how the same
``agent_helpers.llm`` abstraction wires up additional providers (API key,
Ollama, LiteLLM) — the openai-agents SDK needs per-provider model
branching, so this tutorial keeps the surface small and only exercises the
tested paths.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from agent_helpers.llm.spec import ModelSpec

LOGGER = logging.getLogger(__name__)

def _make_azure_model(spec: ModelSpec) -> Any:
    """Build an Azure OpenAI model backed by ``AsyncAzureOpenAI``.

    Defaults to ``OpenAIResponsesModel`` (``/responses``) because the dated
    Azure deployments we test against (``gpt-5.2-codex_*``, ``gpt-5.1_*``) are
    Responses-only and return a clean 404 on ``/chat/completions``. Set
    ``AZURE_OPENAI_API_KIND=chat_completions`` to fall back to
    ``OpenAIChatCompletionsModel`` for older gateways that don't expose
    ``/responses``.
    """
    # openai-agents tries to ship traces to OpenAI's platform tracing endpoint
    # (api.openai.com/v1/traces), which requires a real OpenAI **platform** API
    # key — distinct from your Azure OpenAI key or Entra token. When the agent
    # runs against Azure, every turn raises a 401/403 from the tracing exporter
    # even though the model call itself succeeded, which looks like a "license"
    # or auth failure in the logs. Disable tracing globally for this Azure path
    # so the SDK doesn't try to export anything.
    #
    # See: https://community.openai.com/t/agents-sdk-with-azure-hosted-models/1157781
    try:
        from agents import set_tracing_disabled

        set_tracing_disabled(disabled=True)
    except ImportError:  # pragma: no cover — openai-agents not installed yet
        pass

    from openai import AsyncAzureOpenAI

    client_kwargs: dict[str, Any] = {
        "azure_endpoint": spec.endpoint,
        "api_version": spec.api_version,
    }
    if spec.api_key:
        client_kwargs["api_key"] = spec.api_key
    else:
        # Entra: ``spec.credential_factory()`` returns a zero-arg callable that
        # yields a fresh bearer token string — exactly the shape
        # ``AsyncAzureOpenAI(azure_ad_token_provider=...)`` expects.
        assert spec.credential_factory is not None  # for type-checkers
        client_kwargs["azure_ad_token_provider"] = spec.credential_factory()

    client = AsyncAzureOpenAI(**client_kwargs)

    api_kind = os.getenv("AZURE_OPENAI_API_KIND", "responses").lower()
    if api_kind == "chat_completions":
        from agents import OpenAIChatCompletionsModel

        return OpenAIChatCompletionsModel(model=spec.model, openai_client=client)

    from agents import OpenAIResponsesModel

    return OpenAIResponsesModel(model=spec.model, openai_client=client)


def build_model() -> Any:
    """Dispatch on ``$LLM_PROVIDER`` and return an ``Agent(model=...)``-compatible value.

    Returns
    -------
    Any
        Either a bare model id string (public OpenAI) or an openai-agents
        ``Model`` subclass. Pass directly to ``agents.Agent(model=...)``.
    """
    provider = os.getenv("LLM_PROVIDER", "azure_openai_entra")
    LOGGER.info("Building openai-agents model with provider=%s", provider)

    if provider in ("azure_openai_entra", "azure_openai"):
        spec = ModelSpec.from_env(provider="azure_openai", auth_mode="entra")
        return _make_azure_model(spec)

    if provider == "openai":
        # Public OpenAI: Agent(model="gpt-4o") just works. We still load the
        # spec to validate that OPENAI_API_KEY is set in the environment;
        # the openai-agents SDK reads it from the env directly.
        spec = ModelSpec.from_env(provider="openai")
        return spec.model

    raise ValueError(
        f"Unknown LLM_PROVIDER={provider!r}. Supported: azure_openai_entra, openai. "
        "See docs/tutorials/maf_quickstart/chat_client.py for additional providers."
    )
