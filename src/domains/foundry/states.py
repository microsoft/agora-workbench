"""
Foundry domain state vocabulary (stub).

Placeholder for future PRs.
"""

from enum import Enum, unique


@unique
class FoundryState(str, Enum):
    """Controlled vocabulary of Foundry integration states."""


STATE_AFFORDANCES: dict[FoundryState, list[str]] = {}
