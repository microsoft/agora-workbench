"""
LaTeX domain state vocabulary (stub).

Placeholder for future PRs.
"""

from enum import Enum, unique


@unique
class LatexState(str, Enum):
    """Controlled vocabulary of LaTeX document-processing states."""


STATE_AFFORDANCES: dict[LatexState, list[str]] = {}
