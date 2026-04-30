"""Tests for ToolMaker agent and executor construction."""

from unittest.mock import MagicMock, patch


from toolmaker.models import TaskSpec, ImplementationState, ToolPersistence
from toolmaker.executors import (
    ToolMakerLLMExecutor,
    ExplorationHelpHandlerExecutor,
    ExplorationToBuildBridgeExecutor,
    ExplorationToImplementationBridgeExecutor,
    BuildToDecisionBridgeExecutor,
    ImplementationToRegistrationBridgeExecutor,
    UserDecisionExecutor,
    RegistrationHelpHandlerExecutor,
)
from toolmaker.prompts.renderer import (
    render_exploration_prompt,
    render_implementation_prompt,
    render_registration_prompt,
)


class TestPromptRendering:
    """Test that all prompts render without errors."""

    def test_exploration_prompt_renders(self):
        prompt = render_exploration_prompt()
        assert "EXPLORATION" in prompt
        assert "clone" in prompt.lower()

    def test_implementation_prompt_renders(self):
        prompt = render_implementation_prompt()
        assert "IMPLEMENTATION" in prompt
        assert "CodeExecutionServer" in prompt

    def test_registration_prompt_renders(self):
        prompt = render_registration_prompt()
        assert "REGISTRATION" in prompt
        assert "register_domain" in prompt


class TestToolMakerLLMExecutor:
    """Test ToolMakerLLMExecutor construction."""

    def test_construction(self):
        mock_client = MagicMock()
        executor = ToolMakerLLMExecutor(
            chat_client=mock_client,
            system_prompt="test prompt",
            max_iterations=100,
            tools=[],
            executor_id="test_executor",
        )
        assert executor.id == "test_executor"
        assert executor.system_prompt == "test prompt"
        assert executor.max_iterations == 100
        assert executor.retrieved_tools == []

    def test_unique_ids(self):
        mock_client = MagicMock()
        e1 = ToolMakerLLMExecutor(mock_client, "p1", 10, executor_id="a")
        e2 = ToolMakerLLMExecutor(mock_client, "p2", 10, executor_id="b")
        assert e1.id != e2.id


class TestExplorationHelpHandler:
    def test_construction(self):
        mock_executor = MagicMock()
        spec = TaskSpec()
        handler = ExplorationHelpHandlerExecutor(mock_executor, spec)
        assert handler.id == "exploration_help_handler"
        assert handler.task_spec is spec


class TestBridgeExecutors:
    def test_exploration_to_build_bridge(self):
        spec = TaskSpec(tool_name="my_tool")
        state = ImplementationState()
        bridge = ExplorationToBuildBridgeExecutor(spec, state)
        assert bridge.id == "exploration_to_build_bridge"
        assert bridge.task_spec is spec

    def test_exploration_to_build_bridge_backward_compat_alias(self):
        """The old name still works as an alias."""
        spec = TaskSpec(tool_name="my_tool")
        state = ImplementationState()
        bridge = ExplorationToImplementationBridgeExecutor(spec, state)
        assert bridge.id == "exploration_to_build_bridge"

    def test_build_to_decision_bridge(self):
        spec = TaskSpec()
        state = ImplementationState()
        bridge = BuildToDecisionBridgeExecutor(spec, state)
        assert bridge.id == "build_to_decision_bridge"
        assert bridge._forward is False  # starts as False until tests pass

    def test_build_to_decision_bridge_backward_compat_alias(self):
        """The old name still works as an alias."""
        spec = TaskSpec()
        state = ImplementationState()
        bridge = ImplementationToRegistrationBridgeExecutor(spec, state)
        assert bridge.id == "build_to_decision_bridge"


class TestUserDecisionExecutor:
    def test_construction(self):
        mock_build_llm = MagicMock()
        spec = TaskSpec(tool_name="my_tool", domain_name="test")
        state = ImplementationState()
        executor = UserDecisionExecutor(
            build_llm_executor=mock_build_llm,
            task_spec=spec,
            impl_state=state,
        )
        assert executor.id == "user_decision"
        assert executor.task_spec is spec
        assert executor.impl_state is state
        assert executor.build_llm is mock_build_llm

    def test_initial_persistence_is_undecided(self):
        state = ImplementationState()
        assert state.persistence == ToolPersistence.UNDECIDED


class TestRegistrationHelpHandler:
    def test_construction(self):
        mock_reg_llm = MagicMock()
        mock_build_llm = MagicMock()
        spec = TaskSpec()
        state = ImplementationState()
        handler = RegistrationHelpHandlerExecutor(
            registration_llm_executor=mock_reg_llm,
            build_llm_executor=mock_build_llm,
            task_spec=spec,
            impl_state=state,
        )
        assert handler.id == "registration_help_handler"


class TestToolMakerAgentConstruction:
    """Test ToolMakerAgent can be constructed (without actually running it)."""

    @patch("toolmaker.agent.ToolMakerAgent._create_chat_client")
    def test_agent_init(self, mock_create_client):
        mock_create_client.return_value = MagicMock()
        from toolmaker.agent import ToolMakerAgent

        agent = ToolMakerAgent(
            llm="test-model",
            max_iterations=10,
        )
        assert agent.task_spec is not None
        assert agent.impl_state is not None
        assert isinstance(agent.task_spec, TaskSpec)
        assert isinstance(agent.impl_state, ImplementationState)

    @patch("toolmaker.agent.ToolMakerAgent._create_chat_client")
    def test_workflow_builds(self, mock_create_client):
        mock_create_client.return_value = MagicMock()
        from toolmaker.agent import ToolMakerAgent

        agent = ToolMakerAgent(
            llm="test-model",
            max_iterations=10,
        )
        workflow = agent._build_workflow()
        assert workflow is not None
