"""Unit tests for plan management tools."""

import json

import pytest

from ..plan import Plan, StepStatus
from ..plan_tools import create_plan_tools


@pytest.fixture
def plan_and_tools():
    """Create a Plan and its associated FunctionTools."""
    plan = Plan()
    tools = create_plan_tools(plan)
    tool_map = {t.name: t for t in tools}
    return plan, tool_map


def _get_func(tool_map, name):
    """Extract the raw async callable from a FunctionTool."""
    return tool_map[name].func


class TestPlanTools:
    """Tests for the plan management FunctionTools."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_view_plan_empty(self, plan_and_tools):
        plan, tools = plan_and_tools
        result = await _get_func(tools, "view_plan")()
        assert "empty" in result.lower()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_add_step(self, plan_and_tools):
        plan, tools = plan_and_tools
        result = await _get_func(tools, "add_step")("Gather requirements")
        data = json.loads(result)
        assert data["step_id"] == 1
        assert data["description"] == "Gather requirements"
        assert data["status"] == "pending"
        assert len(plan.steps) == 1

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_insert_step(self, plan_and_tools):
        plan, tools = plan_and_tools
        await _get_func(tools, "add_step")("First")
        await _get_func(tools, "add_step")("Third")
        result = await _get_func(tools, "insert_step")(1, "Second")
        data = json.loads(result)
        assert data["description"] == "Second"
        descriptions = [s.description for s in plan.steps]
        assert descriptions == ["First", "Second", "Third"]

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_insert_step_invalid(self, plan_and_tools):
        plan, tools = plan_and_tools
        result = await _get_func(tools, "insert_step")(999, "Orphan")
        assert "Error" in result

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_update_step(self, plan_and_tools):
        plan, tools = plan_and_tools
        await _get_func(tools, "add_step")("Original")
        result = await _get_func(tools, "update_step")(1, description="Revised")
        data = json.loads(result)
        assert data["description"] == "Revised"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_update_step_not_found(self, plan_and_tools):
        plan, tools = plan_and_tools
        result = await _get_func(tools, "update_step")(42, description="No such")
        assert "Error" in result

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_set_step_status(self, plan_and_tools):
        plan, tools = plan_and_tools
        await _get_func(tools, "add_step")("Do work")
        result = await _get_func(tools, "set_step_status")(1, "completed", notes="Done")
        data = json.loads(result)
        assert data["status"] == "completed"
        assert data["notes"] == "Done"
        assert plan.steps[0].status == StepStatus.COMPLETED

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_set_step_status_invalid(self, plan_and_tools):
        plan, tools = plan_and_tools
        await _get_func(tools, "add_step")("Step")
        result = await _get_func(tools, "set_step_status")(1, "invalid_status")
        assert "Error" in result
        assert "Invalid status" in result

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_set_step_status_not_found(self, plan_and_tools):
        plan, tools = plan_and_tools
        result = await _get_func(tools, "set_step_status")(99, "completed")
        assert "Error" in result

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_remove_step(self, plan_and_tools):
        plan, tools = plan_and_tools
        await _get_func(tools, "add_step")("To remove")
        result = await _get_func(tools, "remove_step")(1)
        assert "Removed" in result
        assert len(plan.steps) == 0

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_remove_step_not_found(self, plan_and_tools):
        plan, tools = plan_and_tools
        result = await _get_func(tools, "remove_step")(42)
        assert "Error" in result

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_finalize_plan(self, plan_and_tools):
        plan, tools = plan_and_tools
        await _get_func(tools, "add_step")("Step one")
        result = await _get_func(tools, "finalize_plan")()
        assert "finalized" in result.lower()
        assert plan.finalized is True

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_finalize_empty_plan(self, plan_and_tools):
        plan, tools = plan_and_tools
        result = await _get_func(tools, "finalize_plan")()
        assert "empty" in result.lower()
        assert plan.finalized is False

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_view_plan_with_steps(self, plan_and_tools):
        plan, tools = plan_and_tools
        await _get_func(tools, "add_step")("Gather data")
        await _get_func(tools, "add_step")("Analyze results")
        await _get_func(tools, "set_step_status")(1, "completed")
        result = await _get_func(tools, "view_plan")()
        assert "Gather data" in result
        assert "Analyze results" in result
        assert "[✓]" in result

    @pytest.mark.unit
    def test_tool_count(self, plan_and_tools):
        _, tools = plan_and_tools
        expected_names = {
            "view_plan",
            "add_step",
            "insert_step",
            "update_step",
            "set_step_status",
            "remove_step",
            "finalize_plan",
        }
        assert set(tools.keys()) == expected_names
