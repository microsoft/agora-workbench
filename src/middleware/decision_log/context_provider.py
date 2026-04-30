"""Decision log context provider for MAF agents.

This module provides :class:`DecisionLogContextProvider`, a MAF
:class:`~agent_framework.BaseContextProvider` that injects a read-only view
of the :class:`~middleware.decision_log.DecisionLog` into the agent's
context before each run (``before_run``).  This gives the agent visibility
into its own decision history without being able to modify the log.

Recording of decisions is handled separately by
:class:`~middleware.decision_log.DecisionLogChatMiddleware`.  This provider
can optionally hold a reference to the middleware so it can flush pending
synthesis before injecting, ensuring the context is fully up-to-date.
"""

import logging
from typing import Any, Optional

from agent_framework import BaseContextProvider, Message

from .log import DecisionLog

LOGGER = logging.getLogger(__name__)


class DecisionLogContextProvider(BaseContextProvider):
    """MAF context provider that injects a read-only decision log into agent context.

    Before each ``agent.run()`` call, if *inject_context* is ``True``, this
    provider serialises the current log as a ``<decision_log>`` block and
    adds it to the agent's context messages.  If a companion
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

    def __init__(
        self,
        decision_log: DecisionLog,
        *,
        inject_context: bool = True,
        chat_middleware: Optional[Any] = None,
        max_context_entries: int = 20,
    ) -> None:
        super().__init__(source_id="decision_log")
        self._log = decision_log
        self._inject_context = inject_context
        self._chat_middleware = chat_middleware
        self._max_context_entries = max_context_entries

    async def before_run(
        self,
        *,
        agent: Any,
        session: Any,
        context: Any,
        state: dict[str, Any],
    ) -> None:
        """Inject the decision log as read-only context (when enabled).

        When context injection is enabled, any pending background synthesis
        tasks in the companion chat middleware are drained first so the
        injected log is fully up-to-date.

        Args:
            agent: The agent about to run.
            session: The current agent session.
            context: The invocation :class:`~agent_framework.SessionContext`;
                messages are added via ``context.extend_messages``.
            state: Provider-scoped mutable state dict.
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
        context.extend_messages(self, [Message(role="user", text=context_text)])
