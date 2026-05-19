"""Tests for framework-agnostic planning tool descriptor factories."""

import json

import pytest

from planning.models import StepStatus
from planning.store import PlanStore
from planning.tools import (
    create_execution_descriptors,
    create_plan_descriptors,
    create_read_only_descriptors,
)
from code_execution.tools.tool_descriptor import ToolDescriptor


@pytest.fixture
def store():
    s = PlanStore()
    yield s
    s.close()


def _fn(descriptors: list[ToolDescriptor], name: str):
    """Return the func callable for the named descriptor."""
    for d in descriptors:
        if d.name == name:
            return d.func
    raise KeyError(name)


def _by_name(descriptors: list[ToolDescriptor]) -> dict[str, ToolDescriptor]:
    return {d.name: d for d in descriptors}


# ── ToolDescriptor contract ───────────────────────────────────────────────────


class TestDescriptorContract:
    @pytest.mark.unit
    def test_all_plan_descriptors_are_tool_descriptors(self, store):
        for d in create_plan_descriptors(store):
            assert isinstance(d, ToolDescriptor)

    @pytest.mark.unit
    def test_all_have_input_schema(self, store):
        for d in create_plan_descriptors(store):
            assert isinstance(d.input_schema, dict), f"{d.name} missing input_schema"

    @pytest.mark.unit
    def test_all_have_callable_func(self, store):
        for d in create_plan_descriptors(store):
            assert callable(d.func), f"{d.name} func not callable"

    @pytest.mark.unit
    def test_all_have_input_model(self, store):
        """Every descriptor carries its Pydantic input model."""
        from pydantic import BaseModel

        for d in create_plan_descriptors(store):
            assert d.input_model is not None, f"{d.name} missing input_model"
            assert issubclass(d.input_model, BaseModel), f"{d.name} input_model not a BaseModel"

    @pytest.mark.unit
    def test_input_schema_matches_input_model(self, store):
        """input_schema must be derivable from input_model for all descriptors."""
        for d in create_plan_descriptors(store):
            expected = d.input_model.model_json_schema()
            assert d.input_schema == expected, f"{d.name}: input_schema drifted from input_model.model_json_schema()"


# ── Tool-set membership ───────────────────────────────────────────────────────


class TestToolSetMembership:
    @pytest.mark.unit
    def test_full_descriptor_names(self, store):
        names = {d.name for d in create_plan_descriptors(store)}
        expected = {
            "view_plan",
            "query_steps",
            "plan_summary",
            "get_history",
            "set_step_status",
            "update_step_notes",
            "add_step",
            "insert_step",
            "update_step",
            "remove_step",
            "finalize_plan",
            "add_dependency",
            "remove_dependency",
            "tag_step",
            "untag_step",
        }
        assert names == expected

    @pytest.mark.unit
    def test_read_only_descriptor_names(self, store):
        names = {d.name for d in create_read_only_descriptors(store)}
        assert names == {"view_plan", "query_steps", "plan_summary", "get_history"}

    @pytest.mark.unit
    def test_execution_descriptor_names(self, store):
        names = {d.name for d in create_execution_descriptors(store)}
        expected = {
            "view_plan",
            "query_steps",
            "plan_summary",
            "get_history",
            "set_step_status",
            "update_step_notes",
        }
        assert names == expected
        assert "add_step" not in names
        assert "finalize_plan" not in names


# ── Read-only descriptors ─────────────────────────────────────────────────────


class TestReadOnlyDescriptors:
    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_view_plan_empty(self, store):
        fns = create_read_only_descriptors(store)
        result = await _fn(fns, "view_plan")()
        assert "empty" in result.lower()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_plan_summary(self, store):
        fns = create_plan_descriptors(store)
        await _fn(fns, "add_step")("A")
        await _fn(fns, "add_step")("B")
        result = await _fn(fns, "plan_summary")()
        data = json.loads(result)
        assert data["total"] == 2
        assert data["pending"] == 2

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_query_steps_invalid_status(self, store):
        fns = create_read_only_descriptors(store)
        result = await _fn(fns, "query_steps")(status="bogus")
        assert "Error" in result

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_get_history(self, store):
        fns = create_plan_descriptors(store)
        await _fn(fns, "add_step")("Step A")
        result = await _fn(fns, "get_history")()
        data = json.loads(result)
        assert len(data) >= 1
        assert data[0]["action"] == "add_step"


# ── Full plan descriptors ─────────────────────────────────────────────────────


class TestFullPlanDescriptors:
    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_add_step(self, store):
        fns = create_plan_descriptors(store)
        result = await _fn(fns, "add_step")("Gather requirements")
        data = json.loads(result)
        assert data["description"] == "Gather requirements"
        assert data["status"] == "pending"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_set_step_status(self, store):
        fns = create_plan_descriptors(store)
        await _fn(fns, "add_step")("Do work")
        step_id = store.steps[0].step_id
        result = await _fn(fns, "set_step_status")(step_id, "completed", notes="Done")
        data = json.loads(result)
        assert data["status"] == "completed"
        assert store.steps[0].status == StepStatus.COMPLETED

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_remove_step(self, store):
        fns = create_plan_descriptors(store)
        await _fn(fns, "add_step")("To remove")
        step_id = store.steps[0].step_id
        result = await _fn(fns, "remove_step")(step_id)
        assert "Removed" in result
        assert len(store.steps) == 0

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_finalize_plan(self, store):
        fns = create_plan_descriptors(store)
        await _fn(fns, "add_step")("Step one")
        result = await _fn(fns, "finalize_plan")()
        assert "finalized" in result.lower()
        assert store.finalized is True

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_tag_and_untag_step(self, store):
        fns = create_plan_descriptors(store)
        await _fn(fns, "add_step")("Research")
        sid = store.steps[0].step_id
        result = await _fn(fns, "tag_step")(sid, "research")
        assert "research" in result
        assert "research" in store.steps[0].tags

        result = await _fn(fns, "untag_step")(sid, "research")
        assert "removed" in result.lower()
        assert store.steps[0].tags == ()
