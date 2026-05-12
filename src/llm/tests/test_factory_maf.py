"""Tests for the MAF chat-client factory.

Skipped entirely when ``agent_framework`` isn't importable. These tests
construct real ``OpenAIChatClient`` instances but never make network calls.
"""

from __future__ import annotations

import pytest

pytest.importorskip("agent_framework")

from llm import ModelSpec  # noqa: E402
from llm.factories import make_maf_client  # noqa: E402


@pytest.fixture
def azure_spec_apikey() -> ModelSpec:
    return ModelSpec(
        provider="azure_openai",
        model="gpt-4o",
        endpoint="https://x.openai.azure.com",
        api_version="preview",
        api_key="sk-test",
    )


@pytest.fixture
def azure_spec_credential(fake_credential_factory) -> ModelSpec:
    return ModelSpec(
        provider="azure_openai",
        model="gpt-4o",
        endpoint="https://x.openai.azure.com",
        api_version="preview",
        credential_factory=fake_credential_factory,
    )


# ---------------------------------------------------------------------------
# Construction per provider
# ---------------------------------------------------------------------------


class TestConstructionPerProvider:
    def test_azure_openai_apikey(self, azure_spec_apikey):
        from agent_framework.openai import OpenAIChatClient

        client = make_maf_client(azure_spec_apikey)
        assert isinstance(client, OpenAIChatClient)

    def test_azure_openai_credential_factory(self, azure_spec_credential):
        from agent_framework.openai import OpenAIChatClient

        client = make_maf_client(azure_spec_credential)
        assert isinstance(client, OpenAIChatClient)

    def test_openai(self):
        from agent_framework.openai import OpenAIChatClient

        spec = ModelSpec(provider="openai", model="gpt-4o", api_key="sk-fake")
        client = make_maf_client(spec)
        assert isinstance(client, OpenAIChatClient)

    def test_ollama(self):
        from agent_framework.openai import OpenAIChatClient

        spec = ModelSpec(
            provider="ollama",
            model="llama3.1",
            endpoint="http://localhost:11434/v1",
            api_key="ollama",
        )
        client = make_maf_client(spec)
        assert isinstance(client, OpenAIChatClient)

    def test_litellm(self):
        from agent_framework.openai import OpenAIChatClient

        spec = ModelSpec(
            provider="litellm",
            model="claude-3-5-sonnet",
            endpoint="http://localhost:4000",
            api_key="sk-litellm",
        )
        client = make_maf_client(spec)
        assert isinstance(client, OpenAIChatClient)


# ---------------------------------------------------------------------------
# Auth-mode validation
# ---------------------------------------------------------------------------


class TestAuthValidation:
    def test_credential_factory_rejected_for_openai(self, fake_credential_factory):
        spec = ModelSpec(
            provider="openai",
            model="gpt-4o",
            credential_factory=fake_credential_factory,
        )
        with pytest.raises(ValueError, match="requires api_key"):
            make_maf_client(spec)

    def test_credential_factory_rejected_for_ollama(self, fake_credential_factory):
        spec = ModelSpec(
            provider="ollama",
            model="llama3.1",
            endpoint="http://localhost:11434/v1",
            credential_factory=fake_credential_factory,
        )
        with pytest.raises(ValueError, match="requires api_key"):
            make_maf_client(spec)


# ---------------------------------------------------------------------------
# Inference defaults & extras passthrough
# ---------------------------------------------------------------------------


class TestKwargsPassthrough:
    def test_extras_reach_client_kwargs(self, monkeypatch):
        """When spec.extra is non-empty its keys are forwarded to OpenAIChatClient."""
        captured: dict = {}

        # Patch OpenAIChatClient at the import site inside maf.py.
        from agent_framework.openai import OpenAIChatClient

        def fake_init(self, **kwargs):
            captured.update(kwargs)

        monkeypatch.setattr(OpenAIChatClient, "__init__", fake_init)

        spec = ModelSpec(
            provider="openai",
            model="gpt-4o",
            api_key="k",
            temperature=0.7,
            max_tokens=256,
            extra={"timeout": 30},
        )
        make_maf_client(spec)

        assert captured["model"] == "gpt-4o"
        assert captured["api_key"] == "k"
        assert captured["temperature"] == 0.7
        assert captured["max_tokens"] == 256
        assert captured["timeout"] == 30

    def test_inference_defaults_omitted_when_unset(self, monkeypatch):
        captured: dict = {}

        from agent_framework.openai import OpenAIChatClient

        def fake_init(self, **kwargs):
            captured.update(kwargs)

        monkeypatch.setattr(OpenAIChatClient, "__init__", fake_init)

        spec = ModelSpec(provider="openai", model="gpt-4o", api_key="k")
        make_maf_client(spec)

        assert "temperature" not in captured
        assert "max_tokens" not in captured
