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
AuthMode = Literal["auto", "entra", "api_key"]

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
        ``api_key``. See :func:`llm.credentials.default_credential_factory`.
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
        if not self.model or not self.model.strip():
            raise ValueError("ModelSpec.model must be a non-empty string")

        if self.endpoint is not None and not self.endpoint.strip():
            raise ValueError("ModelSpec.endpoint must be a non-empty string when provided")

        if self.api_version is not None and not self.api_version.strip():
            raise ValueError("ModelSpec.api_version must be a non-empty string when provided")

        if self.provider in {"azure_openai", "ollama", "litellm"} and not self.endpoint:
            raise ValueError(f"ModelSpec.endpoint is required for provider {self.provider!r}")

        if self.provider == "azure_openai" and not self.api_version:
            raise ValueError("ModelSpec.api_version is required for provider 'azure_openai'")

        has_key = bool(self.api_key)
        has_factory = self.credential_factory is not None
        if has_key and has_factory:
            raise ValueError("ModelSpec accepts api_key OR credential_factory, not both")
        if not has_key and not has_factory:
            raise ValueError("ModelSpec requires either api_key or credential_factory")

    # ------------------------------------------------------------------
    # Constructors
    # ------------------------------------------------------------------

    @classmethod
    def from_env(
        cls,
        prefix: str = "AZURE_OPENAI",
        *,
        provider: Provider = "azure_openai",
        auth_mode: AuthMode = "auto",
    ) -> "ModelSpec":
        """Build a :class:`ModelSpec` from environment variables.

        ``provider="azure_openai"`` (default) reads:

        * ``{prefix}_ENDPOINT`` \u2014 required
        * ``{prefix}_DEPLOYMENT_NAME`` (preferred) or ``MODEL_DEPLOYMENT_NAME``
        * ``{prefix}_API_KEY`` — see ``auth_mode`` below
        * ``API_VERSION`` — required
        * ``AOAI_SCOPE`` — optional, defaults to the public AOAI scope
        * ``LLM_TEMPERATURE`` / ``LLM_MAX_TOKENS`` — optional inference defaults

        ``auth_mode`` controls Azure OpenAI authentication selection (ignored
        by non-Azure providers, which always use ``api_key``):

        * ``"auto"`` (default) — use ``{prefix}_API_KEY`` if set, otherwise
          fall back to the Entra credential factory.
        * ``"entra"`` — always use the Entra credential factory; ignore any
          ``{prefix}_API_KEY`` in the environment. Useful when callers know
          their intent and don't want a stale env var to silently flip auth.
        * ``"api_key"`` — require ``{prefix}_API_KEY`` to be set; raise
          ``ValueError`` if not.

        ``provider="openai"`` reads:

        * ``OPENAI_API_KEY`` \u2014 required
        * ``OPENAI_MODEL`` \u2014 optional, defaults to ``"gpt-4o"``
        * ``OPENAI_BASE_URL`` \u2014 optional override (e.g. an OpenAI-compatible proxy)

        ``provider="ollama"`` reads:

        * ``OLLAMA_BASE_URL`` \u2014 optional, defaults to ``"http://localhost:11434/v1"``
        * ``OLLAMA_MODEL`` \u2014 optional, defaults to ``"llama3.1"``
        * api_key is hard-coded to ``"ollama"`` (Ollama doesn't validate it
          but the OpenAI client requires a non-empty value).

        ``provider="litellm"`` reads:

        * ``LITELLM_BASE_URL`` \u2014 required
        * ``LITELLM_API_KEY`` \u2014 required
        * ``LITELLM_MODEL`` \u2014 required

        Raises
        ------
        ValueError
            If a required environment variable is missing.
        """
        if provider == "azure_openai":
            return cls._from_env_azure_openai(prefix, auth_mode)
        if provider == "openai":
            return cls._from_env_openai()
        if provider == "ollama":
            return cls._from_env_ollama()
        if provider == "litellm":
            return cls._from_env_litellm()
        raise ValueError(f"Unknown provider: {provider!r}")  # pragma: no cover

    # -- per-provider env loaders -------------------------------------

    @classmethod
    def _from_env_azure_openai(cls, prefix: str, auth_mode: AuthMode) -> "ModelSpec":
        endpoint = _require_env(f"{prefix}_ENDPOINT")
        api_version = _require_env("API_VERSION")
        model = os.getenv(f"{prefix}_DEPLOYMENT_NAME") or _require_env("MODEL_DEPLOYMENT_NAME")
        scope = os.getenv("AOAI_SCOPE", _DEFAULT_AOAI_SCOPE)

        # Resolve auth based on the explicit mode.
        if auth_mode == "entra":
            api_key: str | None = None
        elif auth_mode == "api_key":
            api_key = _require_env(f"{prefix}_API_KEY")
        else:  # "auto" — env-driven heuristic
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
            provider="azure_openai",
            model=model,
            endpoint=endpoint,
            api_version=api_version,
            credential_factory=credential_factory,
            api_key=api_key,
            scope=scope,
            temperature=_parse_float(os.getenv("LLM_TEMPERATURE")),
            max_tokens=_parse_int(os.getenv("LLM_MAX_TOKENS")),
        )

    @classmethod
    def _from_env_openai(cls) -> "ModelSpec":
        return cls(
            provider="openai",
            model=os.getenv("OPENAI_MODEL", "gpt-4o"),
            endpoint=os.getenv("OPENAI_BASE_URL") or None,
            api_key=_require_env("OPENAI_API_KEY"),
            temperature=_parse_float(os.getenv("LLM_TEMPERATURE")),
            max_tokens=_parse_int(os.getenv("LLM_MAX_TOKENS")),
        )

    @classmethod
    def _from_env_ollama(cls) -> "ModelSpec":
        # Ollama doesn't validate the api_key but the OpenAI client requires
        # a non-empty string \u2014 use the conventional "ollama" placeholder.
        return cls(
            provider="ollama",
            model=os.getenv("OLLAMA_MODEL", "llama3.1"),
            endpoint=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
            api_key="ollama",
            temperature=_parse_float(os.getenv("LLM_TEMPERATURE")),
            max_tokens=_parse_int(os.getenv("LLM_MAX_TOKENS")),
        )

    @classmethod
    def _from_env_litellm(cls) -> "ModelSpec":
        return cls(
            provider="litellm",
            model=_require_env("LITELLM_MODEL"),
            endpoint=_require_env("LITELLM_BASE_URL"),
            api_key=_require_env("LITELLM_API_KEY"),
            temperature=_parse_float(os.getenv("LLM_TEMPERATURE")),
            max_tokens=_parse_int(os.getenv("LLM_MAX_TOKENS")),
        )


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"Environment variable {name!r} is required to build a ModelSpec")
    return value


def _parse_float(value: str | None) -> float | None:
    return float(value) if value else None


def _parse_int(value: str | None) -> int | None:
    return int(value) if value else None
