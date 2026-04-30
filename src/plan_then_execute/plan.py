"""
Plan data structure for the plan-then-execute agent.

The plan is an external data structure that the agent manipulates via tools.
It consists of ordered steps, each with a description, status, and optional notes.

Migration note
--------------
``Plan`` is now a thin wrapper over ``PlanStore(":memory:")`` from the
standalone ``planning`` package.  New code should use ``PlanStore`` directly::

    from planning import PlanStore, create_plan_tools

    store = PlanStore()  # or PlanStore("/path/to/plan.db") for persistence
    tools = create_plan_tools(store)

``Plan`` is kept for backwards compatibility with existing code that imports
from this module.
"""

from enum import Enum
from typing import Optional

from planning.store import PlanStore
from planning.models import StepStatus as _StoreStepStatus


class StepStatus(str, Enum):
    """Status of a plan step."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class PlanStep:
    """A single step in the execution plan.

    This is the legacy data-transfer object returned by ``Plan``.  Its
    interface matches the pre-PlanStore implementation so that existing
    callers continue to work without changes.
    """

    def __init__(self, description: str, step_id: Optional[int] = None):
        self.step_id: int = step_id or 0
        self.description: str = description
        self.status: StepStatus = StepStatus.PENDING
        self.notes: str = ""

    def to_dict(self) -> dict:
        return {
            "step_id": self.step_id,
            "description": self.description,
            "status": self.status.value,
            "notes": self.notes,
        }

    def __repr__(self) -> str:
        return f"PlanStep(id={self.step_id}, status={self.status.value}, desc={self.description!r})"


def _record_to_step(record) -> PlanStep:
    """Convert a ``StepRecord`` from PlanStore to the legacy ``PlanStep``."""
    step = PlanStep(description=record.description, step_id=record.step_id)
    step.status = StepStatus(record.status.value)
    step.notes = record.notes
    return step


class Plan:
    """
    External plan data structure for the plan-then-execute agent.

    This class is a thin wrapper over ``PlanStore(":memory:")`` that preserves
    the original interface for backwards compatibility.  Prefer using
    ``PlanStore`` directly for new code.
    """

    def __init__(self):
        self._store = PlanStore()

    def close(self) -> None:
        """Close the underlying PlanStore and its database connection."""
        self._store.close()

    def __enter__(self) -> "Plan":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def finalized(self) -> bool:
        return self._store.finalized

    @finalized.setter
    def finalized(self, value: bool) -> None:
        self._store.finalized = value

    @property
    def steps(self) -> list[PlanStep]:
        return [_record_to_step(r) for r in self._store.steps]

    def add_step(self, description: str) -> PlanStep:
        """Add a new step to the end of the plan."""
        return _record_to_step(self._store.add_step(description))

    def insert_step(self, after_step_id: int, description: str) -> PlanStep:
        """Insert a new step after the given step_id.

        Args:
            after_step_id: The step_id after which to insert. Use 0 to insert at the beginning.
            description: Description of the new step.

        Returns:
            The newly created PlanStep.

        Raises:
            ValueError: If after_step_id is not found (and is not 0).
        """
        return _record_to_step(self._store.insert_step(after_step_id, description))

    def update_step(self, step_id: int, description: Optional[str] = None, notes: Optional[str] = None) -> PlanStep:
        """Update a step's description and/or notes.

        Raises:
            ValueError: If step_id is not found.
        """
        return _record_to_step(self._store.update_step(step_id, description=description, notes=notes))

    def set_step_status(self, step_id: int, status: StepStatus, notes: Optional[str] = None) -> PlanStep:
        """Set the status of a step.

        Raises:
            ValueError: If step_id is not found.
        """
        store_status = _StoreStepStatus(status.value)
        return _record_to_step(self._store.set_step_status(step_id, store_status, notes=notes))

    def remove_step(self, step_id: int) -> PlanStep:
        """Remove a step from the plan.

        Raises:
            ValueError: If step_id is not found.
        """
        return _record_to_step(self._store.remove_step(step_id))

    def view(self) -> str:
        """Return a human-readable view of the plan."""
        return self._store.view()

    def finalize(self) -> str:
        """Mark the plan as finalized (ready for execution)."""
        return self._store.finalize()

    def is_complete(self) -> bool:
        """Check whether all steps have a terminal status (completed, failed, or skipped)."""
        return self._store.is_complete()

    def _find_step(self, step_id: int) -> PlanStep:
        """Find a step by id using the public PlanStore interface."""
        for record in self._store.steps:
            if record.step_id == step_id:
                return _record_to_step(record)
        raise ValueError(f"Step with id {step_id} not found")
