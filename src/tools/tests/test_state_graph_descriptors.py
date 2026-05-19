"""Tests for workflow planning and skill-loader descriptor factories."""

import json
from unittest.mock import patch

import pytest


from tools.tool_descriptor import ToolDescriptor
from tools.search.state_graph_tools import (
    PlanWorkflowInput,
    LoadSkillInput,
    create_plan_workflow_descriptor,
    create_load_skill_descriptor,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EMPTY_TOOLS: list = []
_TEST_SERVER = "testdomain"


def _fake_discover_skills(domains_dir, extra_skill_dirs):
    return [
        {"name": "flowsheet-setup", "abs_path": "/fake/skills/flowsheet-setup/SKILL.md"},
        {"name": "grid-converter", "abs_path": "/fake/skills/grid-converter/SKILL.md"},
    ]


# ---------------------------------------------------------------------------
# PlanWorkflowInput
# ---------------------------------------------------------------------------


class TestPlanWorkflowInput:
    @pytest.mark.unit
    def test_defaults(self):
        inp = PlanWorkflowInput()
        assert inp.domain == ""
        assert inp.mode == "overview"
        assert inp.current_state == ""
        assert inp.target_state == ""
        assert inp.tool_name == ""

    @pytest.mark.unit
    def test_json_schema_shape(self):
        schema = PlanWorkflowInput.model_json_schema()
        assert schema["type"] == "object"
        assert "domain" in schema["properties"]
        assert "mode" in schema["properties"]
        assert "current_state" in schema["properties"]


# ---------------------------------------------------------------------------
# LoadSkillInput
# ---------------------------------------------------------------------------


class TestLoadSkillInput:
    @pytest.mark.unit
    def test_required_field(self):
        schema = LoadSkillInput.model_json_schema()
        assert "skill_name" in schema.get("required", [])


# ---------------------------------------------------------------------------
# create_plan_workflow_descriptor
# ---------------------------------------------------------------------------


class TestPlanWorkflowDescriptor:
    @pytest.mark.unit
    def test_returns_tool_descriptor(self):
        descriptor = create_plan_workflow_descriptor(server_name=_TEST_SERVER, tools=_EMPTY_TOOLS)
        assert isinstance(descriptor, ToolDescriptor)

    @pytest.mark.unit
    def test_descriptor_metadata(self):
        descriptor = create_plan_workflow_descriptor(server_name=_TEST_SERVER, tools=_EMPTY_TOOLS)
        assert descriptor.name == f"plan_{_TEST_SERVER}_workflow"
        assert "workflow" in descriptor.description.lower()

    @pytest.mark.unit
    def test_input_model_is_correct(self):
        descriptor = create_plan_workflow_descriptor(server_name=_TEST_SERVER, tools=_EMPTY_TOOLS)
        assert descriptor.input_model is PlanWorkflowInput

    @pytest.mark.unit
    def test_input_schema_matches_model(self):
        descriptor = create_plan_workflow_descriptor(server_name=_TEST_SERVER, tools=_EMPTY_TOOLS)
        assert descriptor.input_schema == PlanWorkflowInput.model_json_schema()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_overview_mode_returns_json(self):
        descriptor = create_plan_workflow_descriptor(server_name=_TEST_SERVER, tools=_EMPTY_TOOLS)
        raw = await descriptor.func(domain="", mode="overview")
        parsed = json.loads(raw)
        assert isinstance(parsed, dict)

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_invalid_mode_returns_error(self):
        descriptor = create_plan_workflow_descriptor(server_name=_TEST_SERVER, tools=_EMPTY_TOOLS)
        raw = await descriptor.func(domain="", mode="bogus")
        parsed = json.loads(raw)
        assert "error" in parsed
        assert "bogus" in parsed["error"]

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_none_tools_defaults_to_empty(self):
        """When tools=None, the graph is built with an empty tool list."""
        descriptor = create_plan_workflow_descriptor(server_name=_TEST_SERVER, tools=None)
        raw = await descriptor.func(domain="", mode="overview")
        parsed = json.loads(raw)
        assert isinstance(parsed, dict)

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_next_steps_mode(self):
        """The 'next_steps' mode maps to StateGraph.from_state()."""
        descriptor = create_plan_workflow_descriptor(server_name=_TEST_SERVER, tools=_EMPTY_TOOLS)
        raw = await descriptor.func(mode="next_steps", current_state="")
        parsed = json.loads(raw)
        # Empty state should return an error hint
        assert "error" in parsed or "state" in parsed


# ---------------------------------------------------------------------------
# create_load_skill_descriptor
# ---------------------------------------------------------------------------


class TestLoadSkillDescriptor:
    @pytest.mark.unit
    def test_returns_tool_descriptor(self):
        descriptor = create_load_skill_descriptor(server_name=_TEST_SERVER)
        assert isinstance(descriptor, ToolDescriptor)

    @pytest.mark.unit
    def test_descriptor_metadata(self):
        descriptor = create_load_skill_descriptor(server_name=_TEST_SERVER)
        assert descriptor.name == f"load_{_TEST_SERVER}_skill"
        assert "skill" in descriptor.description.lower()

    @pytest.mark.unit
    def test_input_model_is_correct(self):
        descriptor = create_load_skill_descriptor(server_name=_TEST_SERVER)
        assert descriptor.input_model is LoadSkillInput

    @pytest.mark.unit
    def test_input_schema_matches_model(self):
        descriptor = create_load_skill_descriptor(server_name=_TEST_SERVER)
        assert descriptor.input_schema == LoadSkillInput.model_json_schema()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_missing_skill_returns_error_with_available(self):
        with patch(
            "tools.search.state_graph_tools._discover_skills",
            side_effect=_fake_discover_skills,
        ):
            descriptor = create_load_skill_descriptor(server_name=_TEST_SERVER)
            raw = await descriptor.func(skill_name="nonexistent")
            parsed = json.loads(raw)
            assert "error" in parsed
            assert "nonexistent" in parsed["error"]
            assert "available_skills" in parsed
            assert "flowsheet-setup" in parsed["available_skills"]

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_read_failure_returns_error(self):
        with patch(
            "tools.search.state_graph_tools._discover_skills",
            side_effect=_fake_discover_skills,
        ):
            descriptor = create_load_skill_descriptor(server_name=_TEST_SERVER)
            # The fake path doesn't exist, so reading it should fail
            raw = await descriptor.func(skill_name="flowsheet-setup")
            parsed = json.loads(raw)
            assert "error" in parsed
            assert "Failed to read" in parsed["error"]

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_successful_read(self, tmp_path):
        skill_dir = tmp_path / "skills" / "test-skill"
        skill_dir.mkdir(parents=True)
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text("# Test Skill\nDo the thing.", encoding="utf-8")

        def _discover(domains_dir, extra_skill_dirs):
            return [{"name": "test-skill", "abs_path": str(skill_file)}]

        with patch(
            "tools.search.state_graph_tools._discover_skills",
            side_effect=_discover,
        ):
            descriptor = create_load_skill_descriptor(server_name=_TEST_SERVER)
            raw = await descriptor.func(skill_name="test-skill")
            assert "# Test Skill" in raw
            assert "Do the thing." in raw
