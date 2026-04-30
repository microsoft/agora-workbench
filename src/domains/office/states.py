"""
Office domain state vocabulary (stub).

Placeholder for future PRs.  Add state tokens to ``OfficeState`` and
affordance phrases to ``STATE_AFFORDANCES`` as Office tools are annotated
with state transitions.
"""

from enum import Enum, unique


@unique
class OfficeState(str, Enum):
    """Controlled vocabulary of Office document-processing states."""


STATE_AFFORDANCES: dict[OfficeState, list[str]] = {}
