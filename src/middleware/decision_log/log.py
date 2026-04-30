"""Thread-safe, append-only decision log."""

import threading
from collections.abc import Iterator

from .entry import DecisionLogEntry


class DecisionLog:
    """Thread-safe, append-only store of agent decision log entries.

    The log is written to exclusively by MAF middleware (via :meth:`_append`).
    Agents have read-only access through the :attr:`entries` property and
    :meth:`to_context_string` helper.

    Example usage by a programmer::

        from middleware.decision_log import DecisionLog, DecisionLogContextProvider

        log = DecisionLog()
        provider = DecisionLogContextProvider(
            decision_log=log,
            agent_name="planner",
            inject_context=True,
        )
        # Pass provider to the agent's context_providers list
    """

    def __init__(self) -> None:
        self._entries: list[DecisionLogEntry] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Read-only public interface (safe for agents)
    # ------------------------------------------------------------------

    @property
    def entries(self) -> tuple[DecisionLogEntry, ...]:
        """Immutable snapshot of all log entries, ordered oldest-first.

        Returns:
            A tuple of :class:`DecisionLogEntry` objects.
        """
        with self._lock:
            return tuple(self._entries)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def __iter__(self) -> Iterator[DecisionLogEntry]:
        """Iterate over a snapshot of entries (does not hold the lock)."""
        return iter(self.entries)

    def to_context_string(self, max_entries: int = 20) -> str:
        """Format the log as a human-readable string suitable for context injection.

        Args:
            max_entries: Maximum number of most-recent entries to include.
                Pass ``0`` to include all entries.

        Returns:
            A multi-line string representation of the log, or a placeholder
            message when the log is empty.
        """
        entries = self.entries
        if max_entries:
            entries = entries[-max_entries:]

        if not entries:
            return "No decisions recorded yet."

        lines: list[str] = []
        for e in entries:
            lines.append(f"[{e.timestamp}] {e.agent}: {e.summary}")
            if e.evidence:
                evidence_str = ", ".join(f"{k}={v}" for k, v in e.evidence.items())
                lines.append(f"  Evidence: {evidence_str}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Middleware-only write interface (not exposed to agents)
    # ------------------------------------------------------------------

    def _append(self, entry: DecisionLogEntry) -> None:
        """Append an entry to the log.

        This method is intended for use by MAF middleware only (e.g.
        :class:`~middleware.decision_log.DecisionLogContextProvider`).
        It is intentionally prefixed with ``_`` to signal that agents
        should not call it directly.

        Args:
            entry: The :class:`DecisionLogEntry` to append.
        """
        with self._lock:
            self._entries.append(entry)
