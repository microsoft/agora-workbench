"""Tests for ModularAgent hook-based customization."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from .. import modular_agent as _modular_agent_module
from ..modular_agent import ModularAgent
from ..response_models import SolutionResponse


def _named_tool(name: str):
    tool = MagicMock()
    tool.name = name
    return tool


def _make_result(outputs=None, pending=None):
    result = MagicMock()
    result.get_outputs.return_value = outputs or []
    result.get_request_info_events.return_value = pending or []
    return result


@pytest.fixture
def modular_agent(mock_environment_variables):
    with (
        patch("agent_bot.agora.modular_agent.AzureOpenAIChatClient") as mock_client_class,
        patch.object(_modular_agent_module, "render_system_prompt", return_value="Base prompt"),
    ):
        mock_client_class.return_value = MagicMock()
        yield ModularAgent(llm="gpt-4o")


class TestModularAgentHooks:
    @pytest.mark.unit
    def test_build_tools_supports_modulation_hooks(self, mock_environment_variables):
        with (
            patch("agent_bot.agora.modular_agent.AzureOpenAIChatClient") as mock_client_class,
            patch.object(_modular_agent_module, "render_system_prompt", return_value="Base prompt"),
        ):
            mock_client_class.return_value = MagicMock()

            agent = ModularAgent(
                llm="gpt-4o",
                enable_auto_tool_discovery=False,
                auto_tool_discovery=lambda: [_named_tool("auto_tool")],
                enable_auto_skill_discovery=True,
                auto_skill_discovery=lambda: ["grid_skill"],
                search_tool_factory=lambda _backend_cls, _token: _named_tool("search_tools"),
                skill_search_tool_factory=lambda _skills: _named_tool("search_skills"),
                sub_agent_tool_factories=[lambda: _named_tool("sub_agent_tool")],
                tool_modulator=lambda tools: [*tools, _named_tool("modulated_tool")],
                required_tools=[
                    "auto_tool",
                    "search_tools",
                    "search_skills",
                    "sub_agent_tool",
                    "modulated_tool",
                ],
                skill_advertiser=lambda skills: f"Discovered skills: {', '.join(skills)}",
            )

            tools, errors = agent._build_tools()
            tool_names = {tool.name for tool in tools}

            assert not errors
            assert {
                "auto_tool",
                "search_tools",
                "search_skills",
                "sub_agent_tool",
                "modulated_tool",
            }.issubset(tool_names)
            assert "Discovered skills: grid_skill" in agent.system_prompt

    @pytest.mark.unit
    def test_build_tools_reports_missing_required_tools(self, mock_environment_variables):
        with (
            patch("agent_bot.agora.modular_agent.AzureOpenAIChatClient") as mock_client_class,
            patch.object(_modular_agent_module, "render_system_prompt", return_value="Base prompt"),
        ):
            mock_client_class.return_value = MagicMock()

            agent = ModularAgent(
                llm="gpt-4o",
                enable_auto_tool_discovery=False,
                search_tool_factory=lambda _backend_cls, _token: _named_tool("search_tools"),
                required_tools=["must_have_tool"],
                tool_modulator=lambda _tools: [],
            )

            _, errors = agent._build_tools()
            assert any("Missing required tools: must_have_tool" in err for err in errors)

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_autopilot_mode_skips_input_handler(self, modular_agent):
        modular_agent.autopilot = True

        req_event = MagicMock()
        req_event.request_id = "req-1"
        req_event.data = MagicMock(question="Need more detail")
        first_result = _make_result(pending=[req_event])
        second_result = _make_result(outputs=[SolutionResponse(action="solution", solution="Done")])

        mock_workflow = MagicMock()
        mock_workflow.run = AsyncMock(side_effect=[first_result, second_result])
        modular_agent._build_workflow = AsyncMock(return_value=mock_workflow)

        input_handler = AsyncMock(return_value="user response")
        msg = await modular_agent.go("Analyze this", input_handler=input_handler)

        assert msg.text == "Done"
        input_handler.assert_not_awaited()
        assert mock_workflow.run.await_args_list[1].kwargs["responses"] == {
            "req-1": "Autopilot mode: proceed with best effort using available context and assumptions."
        }
