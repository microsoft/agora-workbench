"""ModelSpec — framework-agnostic LLM configuration.

A :class:`ModelSpec` captures everything a framework factory needs to build a
chat client: endpoint, deployment/model id, API version, credentials, and
inference defaults. Per-framework factories translate it to their native
client types (see ``src/llm/factories/``).

Design notes:
    * The dataclass is frozen so specs are hashable and safely cacheable.
    * Auth is either api-key or a zero-arg ``credential_factory`` callable —
      never both. The factory pattern lets each framework client own a fresh
      credential instance with its own token-refresh lifecycle.
    * ``provider`` is a closed :data:`typing.Literal` of providers the spec
      knows how to model. Factories may raise :class:`NotImplementedError`
      for providers they don't yet support; this is by design.
    * ``extra`` is the escape hatch for framework-specific kwargs (e.g.
      ``response_format``) without growing the core surface.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

Provider = Literal["azure_openai", "openai", "ollama", "litellm"]

# Default OAuth scope for Azure OpenAI when no AOAI_SCOPE env var is set.
# Matches the public Azure Cognitive Services audience.
_DEFAULT_AOAI_SCOPE = "https://cognitiveservices.azure.com/.default"


@dataclass(frozen=True)
class ModelSpec:
    """Declarative LLM configuration consumed by per-framework factories.

    Parameters
    ----------
    provider :
        Identifier for the backing LLM service.
    model :
        Deployment name (Azure OpenAI) or model id (OpenAI / Ollama / LiteLLM).
    endpoint :
        Base URL for the service. Required for ``azure_openai``, ``ollama``,
        and ``litellm``; optional for ``openai`` (defaults to api.openai.com).
    api_version :
        Azure OpenAI API version (e.g. ``"preview"``, ``"2024-10-21"``).
        Ignored by non-Azure providers.
    credential_factory :
        Zero-arg callable returning a credential. Mutually exclusive with
        ``api_key``. See :func:`agora_agent.llm.credentials.default_credential_factory`.
    api_key :
        Static API key. Mutually exclusive with ``credential_factory``.
    scope :
        OAuth scope used when constructing the default credential factory via
        :meth:`from_env`. Carried on the spec for traceability; factories
        themselves don't consume it (the factory closes over the scope).
    temperature, max_tokens :
        Optional inference defaults. Factories pass them through only when
        non-``None``.
    extra :
        Framework-specific passthrough kwargs.
    """

    provider: Provider
    model: str
    endpoint: str | None = None
    api_version: str | None = None
    credential_factory: Callable[[], Any] | None = None
    api_key: str | None = None
    scope: str = _DEFAULT_AOAI_SCOPE
    temperature: float | None = None
    max_tokens: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.model:
            raise ValueError("ModelSpec.model must be a non-empty string")
        has_key = bool(self.api_key)
        has_factory = self.credential_factory is not None
        if has_key and has_factory:
            raise ValueError(
                "ModelSpec accepts api_key OR credential_factory, not both"
            )
        if not has_key and not has_factory:
            raise ValueError(
                "ModelSpec requires either api_key or credential_factory"
            )

    # ------------------------------------------------------------------
    # Constructors
    # ------------------------------------------------------------------

    @classmethod
    def from_env(
        cls,
        prefix: str = "AZURE_OPENAI",
        *,
        provider: Provider = "azure_openai",
    ) -> "ModelSpec":
        """Build a :class:`ModelSpec` from environment variables.

        Reads (for the default ``azure_openai`` provider):

        * ``{prefix}_ENDPOINT`` — required
        * ``{prefix}_DEPLOYMENT_NAME`` (preferred) or ``MODEL_DEPLOYMENT_NAME``
        * ``{prefix}_API_KEY`` — optional; when set, key auth is used
        * ``API_VERSION`` — required
        * ``AOAI_SCOPE`` — optional, defaults to the public AOAI scope
        * ``LLM_TEMPERATURE`` / ``LLM_MAX_TOKENS`` — optional inference defaults

        If ``{prefix}_API_KEY`` is set, ``credential_factory`` is left as
        ``None`` and the key is used directly. Otherwise the default
        credential factory (Entra ID via the agora auth chain) is wired up
        with the configured ``scope``.

        Raises
        ------
        ValueError
            If a required environment variable is missing.
        NotImplementedError
            If ``provider`` is anything other than ``"azure_openai"`` — other
            providers will be supported as factories for them land.
        """
        if provider != "azure_openai":
            raise NotImplementedError(
                f"ModelSpec.from_env currently supports provider='azure_openai' "
                f"only (got {provider!r}). Construct ModelSpec directly for "
                "other providers."
            )

        endpoint = _require_env(f"{prefix}_ENDPOINT")
        api_version = _require_env("API_VERSION")
        model = os.getenv(f"{prefix}_DEPLOYMENT_NAME") or _require_env(
            "MODEL_DEPLOYMENT_NAME"
        )
        scope = os.getenv("AOAI_SCOPE", _DEFAULT_AOAI_SCOPE)
        api_key = os.getenv(f"{prefix}_API_KEY") or None

        credential_factory: Callable[[], Any] | None
        if api_key:
            credential_factory = None
        else:
            # Import here to keep the spec module free of an auth dependency
            # at import time; tests that don't exercise the default factory
            # never trigger this import.
            from .credentials import default_credential_factory

            credential_factory = default_credential_factory(scope)

        return cls(
            provider=provider,
            model=model,
            endpoint=endpoint,
            api_version=api_version,
            credential_factory=credential_factory,
            api_key=api_key,
            scope=scope,
            temperature=_parse_float(os.getenv("LLM_TEMPERATURE")),
            max_tokens=_parse_int(os.getenv("LLM_MAX_TOKENS")),
        )


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(
            f"Environment variable {name!r} is required to build a ModelSpec"
        )
    return value


def _parse_float(value: str | None) -> float | None:
    return float(value) if value else None


def _parse_int(value: str | None) -> int | None:
    return int(value) if value else None
