"""Immutable decision log entry schema."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any


@dataclass(frozen=True)
class DecisionLogEntry:
    """Immutable record of an agent's semantic decision.

    Attributes:
        timestamp: ISO 8601 UTC timestamp when the decision was made.
        agent: Name of the agent that made the decision.
        summary: Human-readable summary of the decision (agent's explanation).
        evidence: Supporting evidence such as tool names, error codes, or
            status values. Stored as a read-only mapping. Defaults to an
            empty mapping.
    """

    timestamp: str
    agent: str
    summary: str
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.evidence, MappingProxyType):
            object.__setattr__(self, "evidence", MappingProxyType(self.evidence))

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation of this entry.

        Returns:
            Dictionary with ``timestamp``, ``agent``, ``summary``, and
            ``evidence`` keys.
        """
        return {
            "timestamp": self.timestamp,
            "agent": self.agent,
            "summary": self.summary,
            "evidence": dict(self.evidence),
        }
