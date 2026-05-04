"""Decision log context provider.

This module provides :class:`DecisionLogContextProvider`, an Agora
:class:`~middleware.protocols.ContextProvider` that injects a read-only view
of the :class:`~middleware.decision_log.DecisionLog` into the agent's
context before each run.  This gives the agent visibility into its own
decision history without being able to modify the log.

The provider is framework-agnostic.  To use it inside a MAF agent, wrap it
with :func:`~middleware.decision_log.adapters.maf_protocols.wrap_context_provider`:

    from middleware.decision_log import DecisionLog
    from middleware.decision_log.adapters import DecisionLogContextProvider
    from middleware.decision_log.adapters.maf_protocols import wrap_context_provider

    log = DecisionLog()
    provider = DecisionLogContextProvider(log)
    maf_provider = wrap_context_provider(provider)
    agent = Agent(..., context_providers=[maf_provider])

Recording of decisions is handled separately by
:class:`~middleware.decision_log.DecisionLogChatMiddleware`.  This provider
can optionally hold a reference to the middleware so it can flush pending
synthesis before injecting, ensuring the context is fully up-to-date.
"""

import logging
from typing import Any, Optional

from middleware.protocols import AgentContext, ContextProvider, Message

from ..log import DecisionLog

LOGGER = logging.getLogger(__name__)


class DecisionLogContextProvider(ContextProvider):
    """Agora ContextProvider that injects a read-only decision log into agent context.

    Implements :class:`~middleware.protocols.ContextProvider` — wrap it with
    :func:`~middleware.decision_log.adapters.maf_protocols.wrap_context_provider`
    to use it inside a MAF agent.

    Before each agent run, if *inject_context* is ``True``, this provider
    serialises the current log as a ``<decision_log>`` block and adds it to
    the agent's context messages.  If a companion
    :class:`~middleware.decision_log.DecisionLogChatMiddleware` is provided,
    its pending synthesis queue is flushed first to ensure the log is
    up-to-date.

    Args:
        decision_log: The :class:`~middleware.decision_log.DecisionLog`
            instance to read from.
        inject_context: When ``True``, inject a read-only view of the log
            into the agent's context before each run.  Defaults to ``True``.
        chat_middleware: Optional companion
            :class:`~middleware.decision_log.DecisionLogChatMiddleware`
            whose ``flush()`` is called before injecting context.
        max_context_entries: Maximum number of recent log entries to include
            when injecting context.  Defaults to ``20``.
    """

    source_id: str = "decision_log"

    def __init__(
        self,
        decision_log: DecisionLog,
        *,
        inject_context: bool = True,
        chat_middleware: Optional[Any] = None,
        max_context_entries: int = 20,
    ) -> None:
        self._log = decision_log
        self._inject_context = inject_context
        self._chat_middleware = chat_middleware
        self._max_context_entries = max_context_entries

    async def provide(self, context: AgentContext) -> None:
        """Inject the decision log as read-only context (when enabled).

        When context injection is enabled, any pending background synthesis
        tasks in the companion chat middleware are drained first so the
        injected log is fully up-to-date.

        Args:
            context: The :class:`~middleware.protocols.AgentContext` to
                extend with the decision log.
        """
        if not self._inject_context:
            return

        if self._chat_middleware is not None:
            await self._chat_middleware.flush()

        log_str = self._log.to_context_string(self._max_context_entries)
        context_text = (
            "<decision_log>\n"
            "The following is a read-only record of past decisions made by agents. "
            "You may use this for reference but cannot modify it.\n\n"
            f"{log_str}\n"
            "</decision_log>"
        )
        context.extend_messages(self.source_id, [Message(role="user", content=context_text)])
