"""
GIS domain state vocabulary (stub).

Placeholder for future PRs.  Add state tokens to ``GisState`` and
affordance phrases to ``STATE_AFFORDANCES`` as GIS tools are annotated
with state transitions.
"""

from enum import Enum, unique


@unique
class GisState(str, Enum):
    """Controlled vocabulary of GIS analysis states."""


STATE_AFFORDANCES: dict[GisState, list[str]] = {}
