"""Middleware components for Agora Workbench.

Framework-agnostic middleware protocols and supporting data structures.
See ``middleware.protocols`` for the protocol definitions and
``middleware.decision_log`` / ``middleware.tool_learning`` for implementations.

MAF adapters are available when the ``maf`` extra is installed::

    pip install agora-workbench[maf]

    from middleware.decision_log.adapters import DecisionLogChatMiddleware
    from middleware.tool_learning.adapters import VignetteRunMiddleware
"""

from .decision_log import (
    DecisionLog,
    DecisionLogEntry,
)

__all__ = [
    "DecisionLog",
    "DecisionLogEntry",
]
