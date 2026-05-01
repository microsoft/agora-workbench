"""
planning — standalone SQLite-backed plan management package.

Core classes:

    from planning import PlanStore, StepStatus, StepRecord, HistoryRecord

Quick start
-----------
    store = PlanStore()                  # in-memory (ephemeral)
    store = PlanStore("/tmp/plan.db")    # file-backed (persistent)
    store = PlanStore.load("/tmp/plan.db", plan_id="<uuid>")  # resume
"""

from .models import HistoryRecord, StepRecord, StepStatus
from .store import PlanStore

__all__ = [
    # Core data model
    "StepStatus",
    "StepRecord",
    "HistoryRecord",
    # Persistence
    "PlanStore",
]
