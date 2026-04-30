"""
Core data models for the planning package.

These models are framework-agnostic and have no dependency on agent_framework
or any persistence layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional


class StepStatus(str, Enum):
    """Status of a plan step."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class StepRecord:
    """An immutable snapshot of a plan step as returned by PlanStore."""

    step_id: int
    plan_id: str
    description: str
    status: StepStatus
    notes: str
    order_index: int
    tags: tuple[str, ...] = ()
    depends_on: tuple[int, ...] = ()
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        return {
            "step_id": self.step_id,
            "plan_id": self.plan_id,
            "description": self.description,
            "status": self.status.value,
            "notes": self.notes,
            "order_index": self.order_index,
            "tags": list(self.tags),
            "depends_on": list(self.depends_on),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


@dataclass
class HistoryRecord:
    """An entry in the step change-history log."""

    history_id: int
    plan_id: str
    step_id: Optional[int]
    action: str
    data: str
    timestamp: datetime

    def to_dict(self) -> dict:
        return {
            "history_id": self.history_id,
            "plan_id": self.plan_id,
            "step_id": self.step_id,
            "action": self.action,
            "data": self.data,
            "timestamp": self.timestamp.isoformat(),
        }
