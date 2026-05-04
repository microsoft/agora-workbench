"""
Framework-agnostic planning tool descriptor factories.

Provides factory functions that return
:class:`~tools.tool_descriptor.ToolDescriptor` objects for the planning tools.
No agent-framework imports — this module can be used with any framework.

MAF users should import from ``planning.adapters.maf`` which wraps these
descriptors in ``FunctionTool`` objects.

Factory presets
---------------
- :func:`create_plan_descriptors`      — full read/write (planning + execution)
- :func:`create_read_only_descriptors` — view + query only (presentation stage)
- :func:`create_execution_descriptors` — status updates + view, no structural changes
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from pydantic import BaseModel, Field

from .models import StepStatus
from .store import PlanStore
from tools.tool_descriptor import ToolDescriptor

LOGGER = logging.getLogger(__name__)


# ── Pydantic input models (framework-agnostic) ────────────────────────────────


class ViewPlanInput(BaseModel):
    """Input for view_plan (no parameters needed)."""

    pass


class AddStepInput(BaseModel):
    description: str = Field(description="Description of the new plan step.")


class InsertStepInput(BaseModel):
    after_step_id: int = Field(description="Insert the new step after this step_id. Use 0 to insert at the beginning.")
    description: str = Field(description="Description of the new plan step.")


class UpdateStepNotesInput(BaseModel):
    step_id: int = Field(description="ID of the step to update.")
    notes: str = Field(description="Notes to attach to the step.")


class UpdateStepInput(BaseModel):
    step_id: int = Field(description="ID of the step to update.")
    description: Optional[str] = Field(default=None, description="New description for the step.")
    notes: Optional[str] = Field(default=None, description="Notes to attach to the step.")


class SetStepStatusInput(BaseModel):
    step_id: int = Field(description="ID of the step to update.")
    status: str = Field(description="New status: 'pending', 'in_progress', 'completed', 'failed', or 'skipped'.")
    notes: Optional[str] = Field(default=None, description="Optional notes (e.g. failure reason).")


class RemoveStepInput(BaseModel):
    step_id: int = Field(description="ID of the step to remove.")


class FinalizePlanInput(BaseModel):
    """Input for finalize_plan (no parameters needed)."""

    pass


class QueryStepsInput(BaseModel):
    status: Optional[str] = Field(
        default=None,
        description="Filter by status: 'pending', 'in_progress', 'completed', 'failed', 'skipped'.",
    )
    tag: Optional[str] = Field(default=None, description="Filter by tag label.")
    ready_only: bool = Field(
        default=False,
        description="If true, only return steps whose dependencies are all completed.",
    )


class AddDependencyInput(BaseModel):
    step_id: int = Field(description="The dependent step (blocked until depends_on completes).")
    depends_on: int = Field(description="The prerequisite step ID.")


class RemoveDependencyInput(BaseModel):
    step_id: int = Field(description="The dependent step ID.")
    depends_on: int = Field(description="The prerequisite step ID to remove.")


class TagStepInput(BaseModel):
    step_id: int = Field(description="ID of the step to tag.")
    tag: str = Field(description="Label to attach (e.g. 'research', 'implementation').")


class UntagStepInput(BaseModel):
    step_id: int = Field(description="ID of the step to untag.")
    tag: str = Field(description="Label to remove.")


class GetHistoryInput(BaseModel):
    step_id: Optional[int] = Field(
        default=None,
        description="Step ID to retrieve history for. Omit to get full plan history.",
    )


class SummaryInput(BaseModel):
    """Input for plan_summary (no parameters needed)."""

    pass


# ── Helper ────────────────────────────────────────────────────────────────────


def _parse_status(status: Optional[str]) -> Optional[StepStatus]:
    if status is None:
        return None
    try:
        return StepStatus(status)
    except ValueError:
        valid = ", ".join(s.value for s in StepStatus)
        raise ValueError(f"Invalid status '{status}'. Must be one of: {valid}")


# ── Factory functions ─────────────────────────────────────────────────────────


def create_plan_descriptors(store: PlanStore) -> list[ToolDescriptor]:
    """Create the full set of read/write plan tool descriptors bound to *store*.

    Includes structural tools (add/insert/remove steps, dependencies, tags)
    as well as status-update and query tools.  Suitable for planning and
    execution stages.
    """
    read_only = create_read_only_descriptors(store)
    execution = _build_execution_only_descriptors(store)
    structural = _build_structural_descriptors(store)
    dep_tag = _build_dep_tag_descriptors(store)
    return read_only + execution + structural + dep_tag


def create_read_only_descriptors(store: PlanStore) -> list[ToolDescriptor]:
    """Create read-only descriptors: view_plan, query_steps, plan_summary, get_history."""

    async def view_plan() -> str:
        return store.view()

    async def query_steps(
        status: str | None = None,
        tag: str | None = None,
        ready_only: bool = False,
    ) -> str:
        try:
            status_enum = _parse_status(status)
        except ValueError as e:
            return f"Error: {e}"
        steps = store.query_steps(status=status_enum, tag=tag, ready_only=ready_only)
        return json.dumps([s.to_dict() for s in steps])

    async def plan_summary() -> str:
        return json.dumps(store.summary())

    async def get_history(step_id: int | None = None) -> str:
        records = store.get_history(step_id=step_id)
        return json.dumps([r.to_dict() for r in records])

    return [
        ToolDescriptor(
            name="view_plan",
            description="View the current execution plan with all steps and their statuses.",
            input_schema=ViewPlanInput.model_json_schema(),
            func=view_plan,
        ),
        ToolDescriptor(
            name="query_steps",
            description=(
                "Filter and list plan steps by status, tag, or dependency readiness. "
                "Returns a JSON array of step objects."
            ),
            input_schema=QueryStepsInput.model_json_schema(),
            func=query_steps,
        ),
        ToolDescriptor(
            name="plan_summary",
            description="Return a summary of step counts grouped by status.",
            input_schema=SummaryInput.model_json_schema(),
            func=plan_summary,
        ),
        ToolDescriptor(
            name="get_history",
            description=(
                "Retrieve the change history for a specific step or the whole plan. "
                "Omit step_id to get the full plan history."
            ),
            input_schema=GetHistoryInput.model_json_schema(),
            func=get_history,
        ),
    ]


def create_execution_descriptors(store: PlanStore) -> list[ToolDescriptor]:
    """Create execution-stage descriptors: view, query, status updates.

    No structural changes (no add/insert/remove/dependency/tag tools).
    """
    return create_read_only_descriptors(store) + _build_execution_only_descriptors(store)


def _build_execution_only_descriptors(store: PlanStore) -> list[ToolDescriptor]:
    """Build descriptors that update step status and notes (no structural changes)."""

    async def set_step_status(step_id: int, status: str, notes: str | None = None) -> str:
        try:
            status_enum = StepStatus(status)
        except ValueError:
            return f"Error: Invalid status '{status}'. Must be one of: {', '.join(s.value for s in StepStatus)}"
        try:
            step = store.set_step_status(step_id, status_enum, notes=notes)
            return json.dumps(step.to_dict())
        except ValueError as e:
            return f"Error: {e}"

    async def update_step_notes(step_id: int, notes: str) -> str:
        try:
            step = store.update_step(step_id, notes=notes)
            return json.dumps(step.to_dict())
        except ValueError as e:
            return f"Error: {e}"

    return [
        ToolDescriptor(
            name="set_step_status",
            description=(
                "Set the status of a plan step. "
                "Valid statuses: 'pending', 'in_progress', 'completed', 'failed', 'skipped'."
            ),
            input_schema=SetStepStatusInput.model_json_schema(),
            func=set_step_status,
        ),
        ToolDescriptor(
            name="update_step_notes",
            description="Attach or update notes on a step (e.g. progress, failure reason).",
            input_schema=UpdateStepNotesInput.model_json_schema(),
            func=update_step_notes,
        ),
    ]


def _build_structural_descriptors(store: PlanStore) -> list[ToolDescriptor]:
    """Build descriptors that structurally modify the plan (add/insert/update/remove/finalize)."""

    async def add_step(description: str) -> str:
        step = store.add_step(description)
        return json.dumps(step.to_dict())

    async def insert_step(after_step_id: int, description: str) -> str:
        try:
            step = store.insert_step(after_step_id, description)
            return json.dumps(step.to_dict())
        except ValueError as e:
            return f"Error: {e}"

    async def update_step(step_id: int, description: str | None = None, notes: str | None = None) -> str:
        try:
            step = store.update_step(step_id, description=description, notes=notes)
            return json.dumps(step.to_dict())
        except ValueError as e:
            return f"Error: {e}"

    async def remove_step(step_id: int) -> str:
        try:
            step = store.remove_step(step_id)
            return f"Removed step {step.step_id}: {step.description}"
        except ValueError as e:
            return f"Error: {e}"

    async def finalize_plan() -> str:
        return store.finalize()

    return [
        ToolDescriptor(
            name="add_step",
            description="Add a new step to the end of the execution plan.",
            input_schema=AddStepInput.model_json_schema(),
            func=add_step,
        ),
        ToolDescriptor(
            name="insert_step",
            description="Insert a new step after a given step_id. Use after_step_id=0 to insert at the beginning.",
            input_schema=InsertStepInput.model_json_schema(),
            func=insert_step,
        ),
        ToolDescriptor(
            name="update_step",
            description="Update a step's description and/or notes.",
            input_schema=UpdateStepInput.model_json_schema(),
            func=update_step,
        ),
        ToolDescriptor(
            name="remove_step",
            description="Remove a step from the plan.",
            input_schema=RemoveStepInput.model_json_schema(),
            func=remove_step,
        ),
        ToolDescriptor(
            name="finalize_plan",
            description=(
                "Finalize the plan and transition from the planning phase to the execution phase. "
                "Call this once the plan is complete and approved by the user."
            ),
            input_schema=FinalizePlanInput.model_json_schema(),
            func=finalize_plan,
        ),
    ]


def _build_dep_tag_descriptors(store: PlanStore) -> list[ToolDescriptor]:
    """Build dependency and tag management descriptors."""

    async def add_dependency(step_id: int, depends_on: int) -> str:
        try:
            store.add_dependency(step_id, depends_on)
            return f"Dependency added: step {step_id} now depends on step {depends_on}."
        except ValueError as e:
            return f"Error: {e}"

    async def remove_dependency(step_id: int, depends_on: int) -> str:
        try:
            store.remove_dependency(step_id, depends_on)
            return f"Dependency removed: step {step_id} no longer depends on step {depends_on}."
        except ValueError as e:
            return f"Error: {e}"

    async def tag_step(step_id: int, tag: str) -> str:
        try:
            store.tag_step(step_id, tag)
            return f"Tagged step {step_id} with '{tag}'."
        except ValueError as e:
            return f"Error: {e}"

    async def untag_step(step_id: int, tag: str) -> str:
        try:
            store.untag_step(step_id, tag)
            return f"Removed tag '{tag}' from step {step_id}."
        except ValueError as e:
            return f"Error: {e}"

    return [
        ToolDescriptor(
            name="add_dependency",
            description="Add a dependency between steps: step_id will be blocked until depends_on is completed.",
            input_schema=AddDependencyInput.model_json_schema(),
            func=add_dependency,
        ),
        ToolDescriptor(
            name="remove_dependency",
            description="Remove a dependency edge between two steps.",
            input_schema=RemoveDependencyInput.model_json_schema(),
            func=remove_dependency,
        ),
        ToolDescriptor(
            name="tag_step",
            description="Attach a label/tag to a step (e.g. 'research', 'implementation').",
            input_schema=TagStepInput.model_json_schema(),
            func=tag_step,
        ),
        ToolDescriptor(
            name="untag_step",
            description="Remove a label/tag from a step.",
            input_schema=UntagStepInput.model_json_schema(),
            func=untag_step,
        ),
    ]
