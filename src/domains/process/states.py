"""
Process domain state vocabulary (stub).

Placeholder for future PRs.
"""

from enum import Enum, unique


@unique
class ProcessState(str, Enum):
    """Controlled vocabulary of generic process simulation states."""


STATE_AFFORDANCES: dict[ProcessState, list[str]] = {}
