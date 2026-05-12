"""Framework-agnostic LLM configuration and factories.

This package provides :class:`ModelSpec`, a declarative description of an LLM
configuration (endpoint, deployment, auth, inference params), plus
per-framework factories that consume it.

The goal is to keep credential/endpoint plumbing in one place so each
framework adapter only needs to translate a fully-resolved ``ModelSpec`` into
its own client type.

Currently supported factories:
    - MAF: :func:`agora_agent.llm.factories.maf.make_maf_client`

LangGraph, OpenAI Agents, and other factories will be added when those
framework integrations begin. See ``docs/LLM_ABSTRACTION_PLAN.md``.
"""

from __future__ import annotations

from .credentials import default_credential_factory
from .spec import ModelSpec

__all__ = ["ModelSpec", "default_credential_factory"]
