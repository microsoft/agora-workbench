"""MAF (Microsoft Agent Framework) chat-client factory.

Turns a framework-agnostic :class:`~llm.ModelSpec` into a concrete
``agent_framework.openai.OpenAIChatClient``. As of agent-framework 1.2 this
single client class handles every supported backend; which one is selected
depends on the kwargs:

* ``azure_endpoint`` + ``api_version``  → Azure OpenAI
* ``base_url``                          → any OpenAI-compatible endpoint
                                          (Ollama, LiteLLM, vLLM, …)
* neither                               → public api.openai.com

The import of ``agent_framework`` is deferred to call time so importing
:mod:`llm` doesn't require the optional MAF dependency to be installed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..spec import ModelSpec

if TYPE_CHECKING:  # pragma: no cover
    from agent_framework.openai import OpenAIChatClient


def make_maf_client(spec: ModelSpec) -> "OpenAIChatClient":
    """Build a MAF ``OpenAIChatClient`` from a :class:`ModelSpec`.

    Supports every provider in :data:`~llm.spec.Provider`:

    * ``azure_openai`` — Entra credential or API key
    * ``openai``       — public api.openai.com (api_key required)
    * ``ollama``       — local OpenAI-compatible endpoint (api_key="ollama"
      is conventional since Ollama doesn't validate the key)
    * ``litellm``      — any LiteLLM proxy URL (api_key required)

    Parameters
    ----------
    spec :
        Fully-resolved configuration.

    Returns
    -------
    OpenAIChatClient
        A ready-to-use MAF chat client.
    """
    # Lazy import: keeps `import llm` cheap and avoids a hard dependency on
    # agent-framework for environments that don't use MAF.
    from agent_framework.openai import OpenAIChatClient

    if spec.provider == "azure_openai":
        kwargs: dict[str, Any] = {
            "azure_endpoint": spec.endpoint,
            "api_version": spec.api_version,
            "model": spec.model,
        }
    elif spec.provider in ("openai", "ollama", "litellm"):
        # MAF / openai-python uses ``model`` + optional ``base_url`` for any
        # non-Azure backend. Omitting ``base_url`` yields api.openai.com.
        kwargs = {"model": spec.model}
        if spec.endpoint:
            kwargs["base_url"] = spec.endpoint
    else:  # pragma: no cover — Provider Literal makes this unreachable
        raise NotImplementedError(f"make_maf_client does not support provider={spec.provider!r}")

    # Auth: ModelSpec.__post_init__ guarantees exactly one of these is set.
    # Note: ``credential`` (Entra) only makes sense for Azure OpenAI; the
    # other providers should always carry an api_key (use "ollama" as a
    # dummy for local Ollama, which doesn't validate the value).
    if spec.api_key:
        kwargs["api_key"] = spec.api_key
    else:
        if spec.provider != "azure_openai":
            raise ValueError(
                f"provider={spec.provider!r} requires api_key auth; "
                "credential_factory is only supported for azure_openai."
            )
        assert spec.credential_factory is not None  # for type-checkers
        kwargs["credential"] = spec.credential_factory()

    # Pass inference defaults only when set, so MAF's own defaults still apply
    # for any field the caller didn't pin.
    if spec.temperature is not None:
        kwargs["temperature"] = spec.temperature
    if spec.max_tokens is not None:
        kwargs["max_tokens"] = spec.max_tokens

    # Framework-specific passthroughs (e.g. response_format) come last so the
    # caller can override anything above if they really need to.
    kwargs.update(spec.extra)

    return OpenAIChatClient(**kwargs)
