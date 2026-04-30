"""
OpenLCA domain state vocabulary (stub).

Placeholder for future PRs.
"""

from enum import Enum, unique


@unique
class OpenlcaState(str, Enum):
    """Controlled vocabulary of OpenLCA life-cycle assessment states."""


STATE_AFFORDANCES: dict[OpenlcaState, list[str]] = {}
