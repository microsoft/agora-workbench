"""Decision log — framework-agnostic decision recording.

Provides an immutable, append-only record of agents' semantic decisions.

Re-exports
----------
- :class:`DecisionLogEntry` — immutable schema for a single decision record.
- :class:`DecisionLog` — thread-safe, append-only log store.
"""

from .entry import DecisionLogEntry
from .log import DecisionLog

__all__ = [
    "DecisionLogEntry",
    "DecisionLog",
]
