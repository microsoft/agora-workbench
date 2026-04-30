"""
Power-grid domain state vocabulary (stub).

Placeholder for future PRs.  Add state tokens to ``PowergridState`` and
affordance phrases to ``STATE_AFFORDANCES`` as power-grid tools are
annotated with state transitions.
"""

from enum import Enum, unique


@unique
class PowergridState(str, Enum):
    """Controlled vocabulary of power-grid simulation states."""


STATE_AFFORDANCES: dict[PowergridState, list[str]] = {}
