"""Decision log middleware for MAF agents.

Provides an immutable, append-only record of agents' semantic decisions.
Recording is handled by :class:`DecisionLogChatMiddleware` (a MAF
``ChatMiddleware`` that observes every LLM round-trip and synthesises
entries via a small LLM).  Context injection is handled by
:class:`DecisionLogContextProvider` (a MAF ``BaseContextProvider`` that
injects a read-only view of the log before each agent run).

Re-exports
----------
- :class:`DecisionLogEntry` — immutable schema for a single decision record.
- :class:`DecisionLog` — thread-safe, append-only log store.
- :class:`DecisionLogChatMiddleware` — ChatMiddleware that records decisions.
- :class:`DecisionLogContextProvider` — BaseContextProvider for context injection.
"""

from .chat_middleware import DecisionLogChatMiddleware
from .context_provider import DecisionLogContextProvider
from .entry import DecisionLogEntry
from .log import DecisionLog

__all__ = [
    "DecisionLogEntry",
    "DecisionLog",
    "DecisionLogChatMiddleware",
    "DecisionLogContextProvider",
]
