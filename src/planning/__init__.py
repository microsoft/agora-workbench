"""
planning — standalone SQLite-backed plan management package.

Core classes (no agent_framework dependency):

    from planning import PlanStore, StepStatus, StepRecord, HistoryRecord

FunctionTool factories (requires agent_framework):

    from planning import create_plan_tools, create_read_only_tools, create_execution_tools

Quick start
-----------
    store = PlanStore()                  # in-memory (ephemeral)
    store = PlanStore("/tmp/plan.db")    # file-backed (persistent)
    store = PlanStore.load("/tmp/plan.db", plan_id="<uuid>")  # resume

    tools = create_plan_tools(store)     # full read/write tools for agents
"""

from .models import HistoryRecord, StepRecord, StepStatus
from .store import PlanStore
from .tools import create_execution_tools, create_plan_tools, create_read_only_tools

__all__ = [
    # Core data model
    "StepStatus",
    "StepRecord",
    "HistoryRecord",
    # Persistence
    "PlanStore",
    # Tool factories
    "create_plan_tools",
    "create_read_only_tools",
    "create_execution_tools",
]
