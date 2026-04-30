"""Unit tests for planning FunctionTool factories."""

import json

import pytest

from ..models import StepStatus
from ..store import PlanStore
from ..tools import create_execution_tools, create_plan_tools, create_read_only_tools


@pytest.fixture
def store():
    s = PlanStore()
    yield s
    s.close()


@pytest.fixture
def full_tools(store):
    tools = create_plan_tools(store)
    return store, {t.name: t for t in tools}


@pytest.fixture
def read_only(store):
    tools = create_read_only_tools(store)
    return store, {t.name: t for t in tools}


@pytest.fixture
def exec_tools(store):
    tools = create_execution_tools(store)
    return store, {t.name: t for t in tools}


def _fn(tool_map, name):
    return tool_map[name].func


# ── Tool-set membership ───────────────────────────────────────────────────────


class TestToolSetMembership:
    @pytest.mark.unit
    def test_full_tools_names(self, full_tools):
        _, tools = full_tools
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
        assert set(tools.keys()) == expected

    @pytest.mark.unit
    def test_read_only_tools_names(self, read_only):
        _, tools = read_only
        expected = {"view_plan", "query_steps", "plan_summary", "get_history"}
        assert set(tools.keys()) == expected

    @pytest.mark.unit
    def test_execution_tools_names(self, exec_tools):
        _, tools = exec_tools
        expected = {
            "view_plan",
            "query_steps",
            "plan_summary",
            "get_history",
            "set_step_status",
            "update_step_notes",
        }
        assert set(tools.keys()) == expected
        # Structural tools must NOT be present
        assert "add_step" not in tools
        assert "finalize_plan" not in tools
        assert "add_dependency" not in tools
        assert "tag_step" not in tools
        assert "update_step" not in tools


# ── Read-only tools ───────────────────────────────────────────────────────────


class TestReadOnlyTools:
    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_view_plan_empty(self, read_only):
        _, tools = read_only
        result = await _fn(tools, "view_plan")()
        assert "empty" in result.lower()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_plan_summary(self, full_tools):
        store, tools = full_tools
        await _fn(tools, "add_step")("A")
        await _fn(tools, "add_step")("B")
        result = await _fn(tools, "plan_summary")()
        data = json.loads(result)
        assert data["total"] == 2
        assert data["pending"] == 2

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_query_steps_by_status(self, full_tools):
        store, tools = full_tools
        await _fn(tools, "add_step")("A")
        await _fn(tools, "add_step")("B")
        step_b_id = store.steps[1].step_id
        await _fn(tools, "set_step_status")(step_b_id, "completed")
        result = await _fn(tools, "query_steps")(status="completed")
        data = json.loads(result)
        assert len(data) == 1
        assert data[0]["status"] == "completed"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_query_steps_invalid_status(self, read_only):
        _, tools = read_only
        result = await _fn(tools, "query_steps")(status="bogus")
        assert "Error" in result

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_get_history(self, full_tools):
        store, tools = full_tools
        await _fn(tools, "add_step")("Step A")
        result = await _fn(tools, "get_history")()
        data = json.loads(result)
        assert len(data) >= 1
        assert data[0]["action"] == "add_step"


# ── Full plan tools ───────────────────────────────────────────────────────────


class TestFullPlanTools:
    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_add_step(self, full_tools):
        store, tools = full_tools
        result = await _fn(tools, "add_step")("Gather requirements")
        data = json.loads(result)
        assert data["description"] == "Gather requirements"
        assert data["status"] == "pending"
        assert len(store.steps) == 1

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_insert_step(self, full_tools):
        store, tools = full_tools
        await _fn(tools, "add_step")("First")
        await _fn(tools, "add_step")("Third")
        first_id = store.steps[0].step_id
        result = await _fn(tools, "insert_step")(first_id, "Second")
        data = json.loads(result)
        assert data["description"] == "Second"
        descriptions = [s.description for s in store.steps]
        assert descriptions == ["First", "Second", "Third"]

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_insert_step_invalid(self, full_tools):
        _, tools = full_tools
        result = await _fn(tools, "insert_step")(9999, "Orphan")
        assert "Error" in result

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_update_step(self, full_tools):
        store, tools = full_tools
        await _fn(tools, "add_step")("Original")
        step_id = store.steps[0].step_id
        result = await _fn(tools, "update_step")(step_id, description="Revised")
        data = json.loads(result)
        assert data["description"] == "Revised"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_update_step_not_found(self, full_tools):
        _, tools = full_tools
        result = await _fn(tools, "update_step")(9999, description="No such")
        assert "Error" in result

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_set_step_status(self, full_tools):
        store, tools = full_tools
        await _fn(tools, "add_step")("Do work")
        step_id = store.steps[0].step_id
        result = await _fn(tools, "set_step_status")(step_id, "completed", notes="Done")
        data = json.loads(result)
        assert data["status"] == "completed"
        assert data["notes"] == "Done"
        assert store.steps[0].status == StepStatus.COMPLETED

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_set_step_status_invalid(self, full_tools):
        store, tools = full_tools
        await _fn(tools, "add_step")("Step")
        step_id = store.steps[0].step_id
        result = await _fn(tools, "set_step_status")(step_id, "invalid_status")
        assert "Error" in result

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_remove_step(self, full_tools):
        store, tools = full_tools
        await _fn(tools, "add_step")("To remove")
        step_id = store.steps[0].step_id
        result = await _fn(tools, "remove_step")(step_id)
        assert "Removed" in result
        assert len(store.steps) == 0

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_remove_step_not_found(self, full_tools):
        _, tools = full_tools
        result = await _fn(tools, "remove_step")(9999)
        assert "Error" in result

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_finalize_plan(self, full_tools):
        store, tools = full_tools
        await _fn(tools, "add_step")("Step one")
        result = await _fn(tools, "finalize_plan")()
        assert "finalized" in result.lower()
        assert store.finalized is True

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_finalize_empty_plan(self, full_tools):
        store, tools = full_tools
        result = await _fn(tools, "finalize_plan")()
        assert "empty" in result.lower()
        assert store.finalized is False


# ── Dependency tools ──────────────────────────────────────────────────────────


class TestDependencyTools:
    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_add_dependency(self, full_tools):
        store, tools = full_tools
        await _fn(tools, "add_step")("A")
        await _fn(tools, "add_step")("B")
        sid_a = store.steps[0].step_id
        sid_b = store.steps[1].step_id
        result = await _fn(tools, "add_dependency")(sid_b, sid_a)
        assert "Dependency added" in result
        assert sid_a in store.steps[1].depends_on

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_remove_dependency(self, full_tools):
        store, tools = full_tools
        await _fn(tools, "add_step")("A")
        await _fn(tools, "add_step")("B")
        sid_a = store.steps[0].step_id
        sid_b = store.steps[1].step_id
        await _fn(tools, "add_dependency")(sid_b, sid_a)
        result = await _fn(tools, "remove_dependency")(sid_b, sid_a)
        assert "removed" in result.lower()
        assert store.steps[1].depends_on == ()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_add_dependency_cycle(self, full_tools):
        store, tools = full_tools
        await _fn(tools, "add_step")("A")
        await _fn(tools, "add_step")("B")
        sid_a = store.steps[0].step_id
        sid_b = store.steps[1].step_id
        await _fn(tools, "add_dependency")(sid_b, sid_a)
        result = await _fn(tools, "add_dependency")(sid_a, sid_b)
        assert "Error" in result
        assert "cycle" in result.lower()


# ── Tag tools ─────────────────────────────────────────────────────────────────


class TestTagTools:
    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_tag_step(self, full_tools):
        store, tools = full_tools
        await _fn(tools, "add_step")("Research")
        sid = store.steps[0].step_id
        result = await _fn(tools, "tag_step")(sid, "research")
        assert "research" in result
        assert "research" in store.steps[0].tags

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_untag_step(self, full_tools):
        store, tools = full_tools
        await _fn(tools, "add_step")("Step")
        sid = store.steps[0].step_id
        await _fn(tools, "tag_step")(sid, "research")
        result = await _fn(tools, "untag_step")(sid, "research")
        assert "removed" in result.lower()
        assert store.steps[0].tags == ()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_tag_nonexistent_step(self, full_tools):
        _, tools = full_tools
        result = await _fn(tools, "tag_step")(9999, "label")
        assert "Error" in result

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_view_plan_shows_tags(self, full_tools):
        store, tools = full_tools
        await _fn(tools, "add_step")("Research")
        sid = store.steps[0].step_id
        await _fn(tools, "tag_step")(sid, "research")
        view = await _fn(tools, "view_plan")()
        assert "research" in view
