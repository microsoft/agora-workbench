"""Tests for ModelSpec dataclass and from_env constructors."""

from __future__ import annotations

import dataclasses

import pytest

from llm import ModelSpec


# ---------------------------------------------------------------------------
# __post_init__ validation
# ---------------------------------------------------------------------------


class TestPostInitValidation:
    def test_empty_model_raises(self):
        with pytest.raises(ValueError, match="model must be a non-empty string"):
            ModelSpec(provider="openai", model="", api_key="k")

    def test_whitespace_only_model_raises(self):
        with pytest.raises(ValueError, match="model must be a non-empty string"):
            ModelSpec(provider="openai", model="   ", api_key="k")

    def test_whitespace_only_endpoint_raises(self):
        with pytest.raises(ValueError, match="endpoint must be a non-empty string"):
            ModelSpec(provider="openai", model="m", api_key="k", endpoint="   ")

    def test_whitespace_only_api_version_raises(self):
        with pytest.raises(ValueError, match="api_version must be a non-empty string"):
            ModelSpec(
                provider="openai", model="m", api_key="k", api_version="   "
            )

    @pytest.mark.parametrize("provider", ["azure_openai", "ollama", "litellm"])
    def test_missing_endpoint_for_provider_raises(self, provider):
        with pytest.raises(ValueError, match="endpoint is required for provider"):
            ModelSpec(
                provider=provider,  # type: ignore[arg-type]  # parametrize uses non-literal strings
                model="m",
                api_key="k",
            )

    def test_missing_api_version_for_azure_openai_raises(self):
        with pytest.raises(ValueError, match="api_version is required"):
            ModelSpec(
                provider="azure_openai",
                model="m",
                api_key="k",
                endpoint="https://x.openai.azure.com",
            )

    def test_both_auth_modes_raises(self):
        with pytest.raises(ValueError, match="api_key OR credential_factory"):
            ModelSpec(
                provider="openai",
                model="m",
                api_key="k",
                credential_factory=lambda: "x",
            )

    def test_no_auth_raises(self):
        with pytest.raises(ValueError, match="requires either api_key"):
            ModelSpec(provider="openai", model="m")

    def test_api_key_only_succeeds(self):
        spec = ModelSpec(provider="openai", model="m", api_key="k")
        assert spec.api_key == "k"
        assert spec.credential_factory is None

    def test_credential_factory_only_succeeds(self):
        spec = ModelSpec(
            provider="azure_openai",
            model="m",
            endpoint="https://x.openai.azure.com",
            api_version="preview",
            credential_factory=lambda: "tok",
        )
        assert spec.api_key is None
        assert spec.credential_factory is not None


# ---------------------------------------------------------------------------
# Frozen / hashable
# ---------------------------------------------------------------------------


class TestFrozen:
    def test_mutation_raises(self):
        spec = ModelSpec(provider="openai", model="m", api_key="k")
        with pytest.raises(dataclasses.FrozenInstanceError):
            spec.model = "other"  # type: ignore[misc]

    def test_equality_on_same_fields(self):
        a = ModelSpec(provider="openai", model="m", api_key="k")
        b = ModelSpec(provider="openai", model="m", api_key="k")
        assert a == b


# ---------------------------------------------------------------------------
# from_env: azure_openai
# ---------------------------------------------------------------------------


class TestFromEnvAzureOpenAI:
    def test_with_api_key(self, clean_env):
        clean_env.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com")
        clean_env.setenv("API_VERSION", "preview")
        clean_env.setenv("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-4o")
        clean_env.setenv("AZURE_OPENAI_API_KEY", "sk-test")

        spec = ModelSpec.from_env()

        assert spec.provider == "azure_openai"
        assert spec.endpoint == "https://x.openai.azure.com"
        assert spec.api_version == "preview"
        assert spec.model == "gpt-4o"
        assert spec.api_key == "sk-test"
        assert spec.credential_factory is None

    def test_with_credential_factory(self, clean_env):
        clean_env.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com")
        clean_env.setenv("API_VERSION", "preview")
        clean_env.setenv("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-4o")
        # No API key -> credential factory path

        spec = ModelSpec.from_env()

        assert spec.api_key is None
        assert spec.credential_factory is not None

    def test_falls_back_to_model_deployment_name(self, clean_env):
        clean_env.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com")
        clean_env.setenv("API_VERSION", "preview")
        clean_env.setenv("MODEL_DEPLOYMENT_NAME", "fallback-model")
        clean_env.setenv("AZURE_OPENAI_API_KEY", "k")

        spec = ModelSpec.from_env()

        assert spec.model == "fallback-model"

    def test_prefers_aoai_deployment_over_fallback(self, clean_env):
        clean_env.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com")
        clean_env.setenv("API_VERSION", "preview")
        clean_env.setenv("AZURE_OPENAI_DEPLOYMENT_NAME", "preferred")
        clean_env.setenv("MODEL_DEPLOYMENT_NAME", "fallback")
        clean_env.setenv("AZURE_OPENAI_API_KEY", "k")

        spec = ModelSpec.from_env()

        assert spec.model == "preferred"

    def test_custom_aoai_scope_carried_through(self, clean_env):
        clean_env.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com")
        clean_env.setenv("API_VERSION", "preview")
        clean_env.setenv("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-4o")
        clean_env.setenv("AZURE_OPENAI_API_KEY", "k")
        clean_env.setenv("AOAI_SCOPE", "api://custom/.default")

        spec = ModelSpec.from_env()

        assert spec.scope == "api://custom/.default"

    def test_default_scope_when_unset(self, clean_env):
        clean_env.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com")
        clean_env.setenv("API_VERSION", "preview")
        clean_env.setenv("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-4o")
        clean_env.setenv("AZURE_OPENAI_API_KEY", "k")

        spec = ModelSpec.from_env()

        assert spec.scope == "https://cognitiveservices.azure.com/.default"

    def test_missing_endpoint_raises(self, clean_env):
        clean_env.setenv("API_VERSION", "preview")
        clean_env.setenv("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-4o")
        clean_env.setenv("AZURE_OPENAI_API_KEY", "k")

        with pytest.raises(ValueError, match="AZURE_OPENAI_ENDPOINT"):
            ModelSpec.from_env()

    def test_missing_api_version_raises(self, clean_env):
        clean_env.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com")
        clean_env.setenv("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-4o")
        clean_env.setenv("AZURE_OPENAI_API_KEY", "k")

        with pytest.raises(ValueError, match="API_VERSION"):
            ModelSpec.from_env()

    def test_missing_deployment_raises(self, clean_env):
        clean_env.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com")
        clean_env.setenv("API_VERSION", "preview")
        clean_env.setenv("AZURE_OPENAI_API_KEY", "k")

        with pytest.raises(ValueError, match="MODEL_DEPLOYMENT_NAME"):
            ModelSpec.from_env()


class TestFromEnvAzureAuthMode:
    """Explicit auth_mode overrides the env-driven heuristic."""

    def _set_required_azure(self, env):
        env.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com")
        env.setenv("API_VERSION", "preview")
        env.setenv("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-4o")

    def test_entra_ignores_stale_api_key(self, clean_env):
        self._set_required_azure(clean_env)
        clean_env.setenv("AZURE_OPENAI_API_KEY", "stale-key")

        spec = ModelSpec.from_env(auth_mode="entra")

        assert spec.api_key is None
        assert spec.credential_factory is not None

    def test_api_key_required_when_explicit(self, clean_env):
        self._set_required_azure(clean_env)
        # No AZURE_OPENAI_API_KEY set

        with pytest.raises(ValueError, match="AZURE_OPENAI_API_KEY"):
            ModelSpec.from_env(auth_mode="api_key")

    def test_api_key_uses_env(self, clean_env):
        self._set_required_azure(clean_env)
        clean_env.setenv("AZURE_OPENAI_API_KEY", "sk-test")

        spec = ModelSpec.from_env(auth_mode="api_key")

        assert spec.api_key == "sk-test"
        assert spec.credential_factory is None

    def test_auto_is_default(self, clean_env):
        """auto behavior is unchanged: env-driven."""
        self._set_required_azure(clean_env)
        clean_env.setenv("AZURE_OPENAI_API_KEY", "sk-test")

        spec = ModelSpec.from_env()  # default auth_mode="auto"

        assert spec.api_key == "sk-test"


# ---------------------------------------------------------------------------
# from_env: openai
# ---------------------------------------------------------------------------


class TestFromEnvOpenAI:
    def test_minimal(self, clean_env):
        clean_env.setenv("OPENAI_API_KEY", "sk-test")

        spec = ModelSpec.from_env(provider="openai")

        assert spec.provider == "openai"
        assert spec.api_key == "sk-test"
        assert spec.model == "gpt-4o"  # default
        assert spec.endpoint is None

    def test_custom_model_and_base_url(self, clean_env):
        clean_env.setenv("OPENAI_API_KEY", "sk-test")
        clean_env.setenv("OPENAI_MODEL", "gpt-4o-mini")
        clean_env.setenv("OPENAI_BASE_URL", "https://proxy.example.com/v1")

        spec = ModelSpec.from_env(provider="openai")

        assert spec.model == "gpt-4o-mini"
        assert spec.endpoint == "https://proxy.example.com/v1"

    def test_missing_api_key_raises(self, clean_env):
        with pytest.raises(ValueError, match="OPENAI_API_KEY"):
            ModelSpec.from_env(provider="openai")


# ---------------------------------------------------------------------------
# from_env: ollama
# ---------------------------------------------------------------------------


class TestFromEnvOllama:
    def test_defaults(self, clean_env):
        spec = ModelSpec.from_env(provider="ollama")

        assert spec.provider == "ollama"
        assert spec.model == "llama3.1"
        assert spec.endpoint == "http://localhost:11434/v1"
        assert spec.api_key == "ollama"

    def test_overrides(self, clean_env):
        clean_env.setenv("OLLAMA_MODEL", "qwen2.5")
        clean_env.setenv("OLLAMA_BASE_URL", "http://other:11434/v1")

        spec = ModelSpec.from_env(provider="ollama")

        assert spec.model == "qwen2.5"
        assert spec.endpoint == "http://other:11434/v1"


# ---------------------------------------------------------------------------
# from_env: litellm
# ---------------------------------------------------------------------------


class TestFromEnvLiteLLM:
    def test_full(self, clean_env):
        clean_env.setenv("LITELLM_BASE_URL", "http://localhost:4000")
        clean_env.setenv("LITELLM_API_KEY", "sk-litellm")
        clean_env.setenv("LITELLM_MODEL", "claude-3-5-sonnet")

        spec = ModelSpec.from_env(provider="litellm")

        assert spec.provider == "litellm"
        assert spec.endpoint == "http://localhost:4000"
        assert spec.api_key == "sk-litellm"
        assert spec.model == "claude-3-5-sonnet"

    @pytest.mark.parametrize(
        "missing", ["LITELLM_BASE_URL", "LITELLM_API_KEY", "LITELLM_MODEL"]
    )
    def test_missing_required_raises(self, clean_env, missing):
        envs = {
            "LITELLM_BASE_URL": "http://localhost:4000",
            "LITELLM_API_KEY": "sk-litellm",
            "LITELLM_MODEL": "claude-3-5-sonnet",
        }
        envs.pop(missing)
        for k, v in envs.items():
            clean_env.setenv(k, v)

        with pytest.raises(ValueError, match=missing):
            ModelSpec.from_env(provider="litellm")


# ---------------------------------------------------------------------------
# Inference defaults
# ---------------------------------------------------------------------------


class TestInferenceDefaults:
    def test_temperature_and_max_tokens_parsed(self, clean_env):
        clean_env.setenv("OPENAI_API_KEY", "k")
        clean_env.setenv("LLM_TEMPERATURE", "0.5")
        clean_env.setenv("LLM_MAX_TOKENS", "1024")

        spec = ModelSpec.from_env(provider="openai")

        assert spec.temperature == 0.5
        assert spec.max_tokens == 1024

    def test_unset_means_none(self, clean_env):
        clean_env.setenv("OPENAI_API_KEY", "k")

        spec = ModelSpec.from_env(provider="openai")

        assert spec.temperature is None
        assert spec.max_tokens is None
