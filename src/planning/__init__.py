"""
planning — standalone SQLite-backed plan management package.

Core classes:

    from planning import PlanStore, StepStatus, StepRecord, HistoryRecord

Framework-agnostic tool descriptors:

    from planning import create_plan_descriptors, create_read_only_descriptors

Quick start
-----------
    store = PlanStore()                  # in-memory (ephemeral)
    store = PlanStore("/tmp/plan.db")    # file-backed (persistent)
    store = PlanStore.load("/tmp/plan.db", plan_id="<uuid>")  # resume
"""

from .models import HistoryRecord, StepRecord, StepStatus
from .store import PlanStore
from .tools import (
    create_execution_descriptors,
    create_plan_descriptors,
    create_read_only_descriptors,
)

__all__ = [
    # Core data model
    "StepStatus",
    "StepRecord",
    "HistoryRecord",
    # Persistence
    "PlanStore",
    # Framework-agnostic descriptor factories
    "create_plan_descriptors",
    "create_read_only_descriptors",
    "create_execution_descriptors",
]
