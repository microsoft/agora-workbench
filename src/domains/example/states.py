"""
Example domain state vocabulary (stub).

Placeholder — mirrors the example domain for documentation purposes.
"""

from enum import Enum, unique


@unique
class ExampleState(str, Enum):
    """Controlled vocabulary of example domain states."""


STATE_AFFORDANCES: dict[ExampleState, list[str]] = {}
