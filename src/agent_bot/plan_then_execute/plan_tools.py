"""
FunctionTools that let the agent view and revise the external plan.

These tools are the agent's only interface to the Plan data structure.
"""

import json
import logging
from typing import Optional

from agent_framework import FunctionTool
from pydantic import BaseModel, Field

from .plan import Plan, StepStatus

LOGGER = logging.getLogger(__name__)


# ── Pydantic input models ────────────────────────────────────────────────


class ViewPlanInput(BaseModel):
    """Input for view_plan (no parameters needed)."""

    pass


class AddStepInput(BaseModel):
    description: str = Field(description="Description of the new plan step.")


class InsertStepInput(BaseModel):
    after_step_id: int = Field(description="Insert the new step after this step_id. Use 0 to insert at the beginning.")
    description: str = Field(description="Description of the new plan step.")


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


# ── Factory ───────────────────────────────────────────────────────────────


def create_plan_tools(plan: Plan) -> list[FunctionTool]:
    """Create a list of FunctionTools bound to the given Plan instance."""

    async def view_plan() -> str:
        """View the current execution plan with step statuses."""
        return plan.view()

    async def add_step(description: str) -> str:
        """Add a new step to the end of the plan."""
        step = plan.add_step(description)
        return json.dumps(step.to_dict())

    async def insert_step(after_step_id: int, description: str) -> str:
        """Insert a new step after a given step_id. Use after_step_id=0 to insert at the beginning."""
        try:
            step = plan.insert_step(after_step_id, description)
            return json.dumps(step.to_dict())
        except ValueError as e:
            return f"Error: {e}"

    async def update_step(step_id: int, description: str | None = None, notes: str | None = None) -> str:
        """Update a step's description and/or notes."""
        try:
            step = plan.update_step(step_id, description=description, notes=notes)
            return json.dumps(step.to_dict())
        except ValueError as e:
            return f"Error: {e}"

    async def set_step_status(step_id: int, status: str, notes: str | None = None) -> str:
        """Set the status of a plan step."""
        try:
            status_enum = StepStatus(status)
        except ValueError:
            return f"Error: Invalid status '{status}'. Must be one of: {', '.join(s.value for s in StepStatus)}"
        try:
            step = plan.set_step_status(step_id, status_enum, notes=notes)
            return json.dumps(step.to_dict())
        except ValueError as e:
            return f"Error: {e}"

    async def remove_step(step_id: int) -> str:
        """Remove a step from the plan."""
        try:
            step = plan.remove_step(step_id)
            return f"Removed step {step.step_id}: {step.description}"
        except ValueError as e:
            return f"Error: {e}"

    async def finalize_plan() -> str:
        """Finalize the plan and move to the execution phase."""
        return plan.finalize()

    return [
        FunctionTool(
            name="view_plan",
            description="View the current execution plan with all steps and their statuses.",
            func=view_plan,
            input_model=ViewPlanInput,
            approval_mode="never_require",
        ),
        FunctionTool(
            name="add_step",
            description="Add a new step to the end of the execution plan.",
            func=add_step,
            input_model=AddStepInput,
            approval_mode="never_require",
        ),
        FunctionTool(
            name="insert_step",
            description=("Insert a new step after a given step_id. Use after_step_id=0 to insert at the beginning."),
            func=insert_step,
            input_model=InsertStepInput,
            approval_mode="never_require",
        ),
        FunctionTool(
            name="update_step",
            description="Update a step's description and/or notes.",
            func=update_step,
            input_model=UpdateStepInput,
            approval_mode="never_require",
        ),
        FunctionTool(
            name="set_step_status",
            description=(
                "Set the status of a plan step. "
                "Valid statuses: 'pending', 'in_progress', 'completed', 'failed', 'skipped'."
            ),
            func=set_step_status,
            input_model=SetStepStatusInput,
            approval_mode="never_require",
        ),
        FunctionTool(
            name="remove_step",
            description="Remove a step from the plan.",
            func=remove_step,
            input_model=RemoveStepInput,
            approval_mode="never_require",
        ),
        FunctionTool(
            name="finalize_plan",
            description=(
                "Finalize the plan and transition from the planning phase to the execution phase. "
                "Call this once the plan is complete and approved by the user."
            ),
            func=finalize_plan,
            input_model=FinalizePlanInput,
            approval_mode="never_require",
        ),
    ]


def create_plan_view_tool(plan: Plan) -> FunctionTool:
    """Create a single view_plan tool for read-only plan access (e.g., presentation stage)."""

    async def view_plan() -> str:
        """View the current execution plan with step statuses."""
        return plan.view()

    return FunctionTool(
        name="view_plan",
        description="View the current execution plan with all steps and their statuses.",
        func=view_plan,
        input_model=ViewPlanInput,
        approval_mode="never_require",
    )
