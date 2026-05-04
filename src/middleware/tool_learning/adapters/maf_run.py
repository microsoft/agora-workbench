"""
VignetteRunMiddleware: injects tool-guardrail context before agent inference.

This module provides :class:`VignetteRunMiddleware`, an Agora
:class:`~middleware.protocols.ContextProvider` that fetches anti-pattern
vignettes from Azure AI Search and prepends a compact guardrails block to
the agent's context so the LLM is aware of known failure modes before
choosing tool arguments.

The middleware is framework-agnostic.  To use it inside a MAF agent, wrap it
with :func:`~middleware.decision_log.adapters.maf_protocols.wrap_context_provider`:

    from middleware.tool_learning.adapters import VignetteRunMiddleware
    from middleware.decision_log.adapters.maf_protocols import wrap_context_provider

    agora_mw = VignetteRunMiddleware(
        config=ToolLearningConfig.from_env(),
        credential=credential,
    )
    maf_provider = wrap_context_provider(agora_mw)
    agent = Agent(..., context_providers=[maf_provider])

By default the middleware discovers tool names from the agent's registered
tools (via :attr:`~middleware.protocols.AgentContext.tools`) at runtime, so
callers don't need to know the tool set in advance.

Vignette results are cached per tool-name set so repeated invocations with the
same tools don't re-query Azure AI Search.
"""

from __future__ import annotations

import asyncio
import logging
from typing import List, Optional, Sequence

from middleware.protocols import AgentContext, ContextProvider, Message

from ..config import ToolLearningConfig
from ..models import Vignette
from ..render import render_guardrails_block
from ..search_repo import SearchVignetteRepo

LOGGER = logging.getLogger(__name__)


class VignetteRunMiddleware(ContextProvider):
    """
    Agora ContextProvider that injects anti-pattern guardrails before inference.

    Implements :class:`~middleware.protocols.ContextProvider` — wrap it with
    :func:`~middleware.decision_log.adapters.maf_protocols.wrap_context_provider`
    to use it inside a MAF agent.

    Before each agent run, it:
      1. Discovers which tools are registered on the agent (or uses an
         explicit override list).
      2. Fetches anti-pattern vignettes for those tools (cached per tool set).
      3. Prepends a compact guardrails snippet as a system message.

    Args:
        config: Agent memory configuration.
        credential: Azure TokenCredential for Search.
        tool_names: Explicit tool names to fetch guardrails for.  When
            *None* (the default), tool names are discovered automatically
            from the agent's registered tools at each invocation via
            :attr:`~middleware.protocols.AgentContext.tools`.
        tenant_id: Optional tenant ID for scope filtering.
        user_id: Optional user ID for scope filtering.
    """

    def __init__(
        self,
        config: ToolLearningConfig,
        credential=None,
        tool_names: Optional[List[str]] = None,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> None:
        self._config = config
        self._explicit_tool_names: list[str] | None = list(tool_names) if tool_names else None
        self._tenant_id = tenant_id
        self._user_id = user_id
        self._search_repo: Optional[SearchVignetteRepo] = None

        # Cache: frozenset(tool_names) → list[Vignette]
        self._cache: dict[frozenset[str], list[Vignette]] = {}

        try:
            self._search_repo = SearchVignetteRepo(config=config, credential=credential)
        except Exception as e:
            LOGGER.warning("VignetteRunMiddleware: search repo unavailable: %s", e)

    # ------------------------------------------------------------------
    # Tool-name resolution
    # ------------------------------------------------------------------

    def _resolve_tool_names(self, context: AgentContext) -> list[str]:
        """Return the tool names to fetch guardrails for.

        Uses the explicit override when set, otherwise discovers from the
        agent's registered tools via :attr:`~middleware.protocols.AgentContext.tools`.
        """
        if self._explicit_tool_names is not None:
            return self._explicit_tool_names
        return [t.name for t in context.tools]

    # ------------------------------------------------------------------
    # Vignette fetching (with cache)
    # ------------------------------------------------------------------

    async def _fetch_vignettes(self, tool_names: Sequence[str]) -> list[Vignette]:
        """Fetch anti-pattern vignettes, returning cached results when available."""
        cache_key = frozenset(tool_names)
        if cache_key in self._cache:
            return self._cache[cache_key]

        all_vignettes: list[Vignette] = []
        for tool_name in tool_names:
            try:
                vignettes = await asyncio.to_thread(
                    self._search_repo.search_vignettes,  # type: ignore[union-attr]
                    f"tool call guardrails for {tool_name}",
                    tool_name,
                    "anti_pattern",
                    None,
                    self._tenant_id,
                    self._user_id,
                )
                all_vignettes.extend(vignettes)
            except Exception as e:
                LOGGER.warning("Failed to fetch guardrails for %s: %s", tool_name, e)

        self._cache[cache_key] = all_vignettes
        return all_vignettes

    # ------------------------------------------------------------------
    # ContextProvider entry point
    # ------------------------------------------------------------------

    async def provide(self, context: AgentContext) -> None:
        """Inject anti-pattern guardrails into the agent's context.

        Discovers tool names from the agent context (or uses an explicit
        list), fetches anti-pattern vignettes (cached), and injects a
        guardrails system message via
        :meth:`~middleware.protocols.AgentContext.extend_messages` when any
        are found.
        """
        tool_names = self._resolve_tool_names(context)

        if self._search_repo and tool_names:
            all_vignettes = await self._fetch_vignettes(tool_names)

            if all_vignettes:
                guardrails_text = render_guardrails_block(all_vignettes)
                if guardrails_text:
                    guardrails_msg = Message(role="system", content=guardrails_text)
                    context.extend_messages("vignette_guardrails", [guardrails_msg])
                    LOGGER.debug("Injected %d guardrail vignettes", len(all_vignettes))
