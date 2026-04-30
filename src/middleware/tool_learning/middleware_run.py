"""
VignetteRunMiddleware: injects tool-guardrail context before agent inference.

This middleware fetches anti-pattern vignettes from Azure AI Search and prepends
a compact guardrails block to the agent's message list so the LLM is aware of
known failure modes before choosing tool arguments.

By default the middleware discovers tool names from the agent's registered tools
at runtime, so callers don't need to know the tool set in advance::

    middleware = VignetteRunMiddleware(
        config=ToolLearningConfig.from_env(),
        credential=credential,
    )
    agent = Agent(..., middleware=[middleware])

Vignette results are cached per tool-name set so repeated invocations with the
same tools don't re-query Azure AI Search.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, List, Optional, Sequence

from agent_framework import AgentContext, AgentMiddleware, Message

from .config import ToolLearningConfig
from .models import Vignette
from .render import render_guardrails_block
from .search_repo import SearchVignetteRepo

LOGGER = logging.getLogger(__name__)


def _extract_tool_names(context: AgentContext) -> list[str]:
    """Extract tool names from the agent's registered tools.

    Reads ``context.agent.default_options["tools"]`` (non-MCP tools) and
    ``context.agent.mcp_tools`` (MCP tools).  Each tool object exposes a
    ``.name`` attribute.
    """
    names: list[str] = []
    agent = context.agent

    for tool in getattr(agent, "default_options", {}).get("tools", []):
        name = getattr(tool, "name", None)
        if name:
            names.append(name)

    for tool in getattr(agent, "mcp_tools", []):
        name = getattr(tool, "name", None)
        if name:
            names.append(name)

    return names


class VignetteRunMiddleware(AgentMiddleware):
    """
    Agent-run middleware that injects anti-pattern guardrails before inference.

    On each agent run, it:
      1. Discovers which tools are registered on the agent (or uses an
         explicit override list).
      2. Fetches anti-pattern vignettes for those tools (cached per tool set).
      3. Prepends a compact guardrails snippet as a system message.
    """

    def __init__(
        self,
        config: ToolLearningConfig,
        credential=None,
        tool_names: Optional[List[str]] = None,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> None:
        """
        Initialize the middleware.

        Args:
            config: Agent memory configuration.
            credential: Azure TokenCredential for Search.
            tool_names: Explicit tool names to fetch guardrails for.  When
                *None* (the default), tool names are discovered automatically
                from the agent's registered tools at each invocation.
            tenant_id: Optional tenant ID for scope filtering.
            user_id: Optional user ID for scope filtering.
        """
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
        agent's registered tools.
        """
        if self._explicit_tool_names is not None:
            return self._explicit_tool_names
        return _extract_tool_names(context)

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
    # Middleware entry point
    # ------------------------------------------------------------------

    async def process(
        self,
        context: AgentContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        """
        Intercept the agent run to inject guardrails.

        Discovers tool names from the agent (or uses an explicit list),
        fetches anti-pattern vignettes (cached), and prepends a guardrails
        system message when any are found.
        """
        tool_names = self._resolve_tool_names(context)

        if self._search_repo and tool_names:
            all_vignettes = await self._fetch_vignettes(tool_names)

            if all_vignettes:
                guardrails_text = render_guardrails_block(all_vignettes)
                if guardrails_text:
                    guardrails_msg = Message(role="system", text=guardrails_text)
                    context.messages = [guardrails_msg] + list(context.messages)
                    LOGGER.debug("Injected %d guardrail vignettes", len(all_vignettes))

        await call_next()
