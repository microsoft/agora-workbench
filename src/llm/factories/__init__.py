"""Per-framework factories that turn a :class:`~llm.ModelSpec` into a
native chat-client object.

Currently shipped:
    * :func:`maf.make_maf_client` — Microsoft Agent Framework

Future factories (LangGraph, OpenAI Agents SDK, Pydantic AI, …) will land in
sibling modules as those framework integrations begin.
"""

from __future__ import annotations

from .maf import make_maf_client

__all__ = ["make_maf_client"]
