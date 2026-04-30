"""
ToolMakerAgent — a four-phase agent that autonomously creates MCP domain servers
from GitHub repositories.

Given a GitHub repository URL and a conversational task description, this agent:
  (1) explores the repo and builds a task specification collaboratively with the user,
  (2) generates domain server code, builds a Docker image, tests the tool, and
      iterates until it passes,
  (3) asks the user whether to keep the tool as reusable (registered) or
      session-only (no registration), and
  (4) if reusable, registers the domain in AgoraAgentMAF's config files and
      presents results for user approval.

The generated domain follows the CodeExecutionServer pattern established by
existing domains (example, powergrid, process, etc.).

Workflow graph
──────────────

  Phase 1 — Exploration
  ┌──────────────────────────────────────────────────────────────┐
  │  ExplorationLLM ─[Help]─► ExplorationHelp ─► ExplCtx ──┐   │
  │       ▲                                                  │   │
  │       └──────────────────────────────────────────────────┘   │
  │              ─[Solution]─► ExplToBuildBridge                 │
  └──────────────────────────┬───────────────────────────────────┘
                             ▼
  Phase 2 — Build & Test
  ┌──────────────────────────────────────────────────────────────┐
  │  BuildLLM ─[Help]─► BuildHelp ─► BuildCtx ─────────────┐   │
  │       ▲                                                  │   │
  │       └──────────────────────────────────────────────────┘   │
  │                ─[Solution]─► BuildToDecisionBridge            │
  └────────────────────────────┬─────────────────────────────────┘
                               ▼
  Phase 3 — User Decision
  ┌──────────────────────────────────────────────────────────────┐
  │  UserDecision                                                │
  │    ├─ "reusable"  → RegistrationLLM (Phase 4)               │
  │    ├─ "session"   → yield_output (terminate, tool stays)     │
  │    └─ revision    → BuildCtx → BuildLLM (loop back)          │
  └────────────────────────────┬─────────────────────────────────┘
                               ▼ (reusable only)
  Phase 4 — Registration
  ┌──────────────────────────────────────────────────────────────┐
  │  RegistrationLLM ─[Help]─► RegistrationHelp                 │
  │       ▲                        │                             │
  │       │                  [accept] → yield_output (terminate) │
  │       │                  [revise] → BuildCtx ────────────────│──► BuildLLM
  │                                                              │
  │                ─[Solution]─► FinalSolution (yield_output)    │
  └──────────────────────────────────────────────────────────────┘
"""

import asyncio
import logging
import os
from typing import Callable, Optional, TYPE_CHECKING

from agent_framework import (
    Message,
    WorkflowBuilder,
    Case,
    Default,
)
from agent_framework.azure import AzureOpenAIChatClient

from auth import create_entra_token_provider
from agora.executors import (
    SolutionHandlerExecutor,
    HelpHandlerExecutor,
)
from agora.response_models import SolutionResponse, HelpResponse

from .executors import (
    ToolMakerLLMExecutor,
    ExplorationHelpHandlerExecutor,
    ExplorationToBuildBridgeExecutor,
    BuildToDecisionBridgeExecutor,
    UserDecisionExecutor,
    RegistrationHelpHandlerExecutor,
    StringToSolutionExecutor,
)
from .models import TaskSpec, ImplementationState, ToolPersistence
from .tools.repo_tools import create_repo_tools
from .tools.codegen_tools import create_codegen_tools, create_task_spec_tools
from .tools.docker_tools import create_docker_tools
from .tools.registration_tools import create_registration_tools
from .prompts.renderer import (
    render_exploration_prompt,
    render_implementation_prompt,
    render_registration_prompt,
)

if TYPE_CHECKING:
    from agent_framework import Workflow

LOGGER = logging.getLogger(__name__)
USER_LOGGER = logging.getLogger("user")


# ---------------------------------------------------------------------------
# Suppress a known cosmetic error from MCP / anyio during shutdown.
# ---------------------------------------------------------------------------
class _MCPAsyncGenCleanupFilter(logging.Filter):
    """Suppress the expected cancel-scope error from MCP async-generator cleanup."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno < logging.ERROR:
            return True
        msg = record.getMessage()
        if "closing of asynchronous generator" in msg and "streamable_http_client" in msg:
            return False
        return True


logging.getLogger("asyncio").addFilter(_MCPAsyncGenCleanupFilter())


class ToolMakerAgent:
    """
    Four-phase agent: explore → build & test → user decision → register.

    Phase 1 (Exploration):   Explore the repo and build a TaskSpec with the user.
    Phase 2 (Build & Test):  Generate domain server code, build, test, iterate.
    Phase 3 (User Decision): Ask whether to make the tool reusable or session-only.
    Phase 4 (Registration):  Register the domain and present results (reusable only).

    The TaskSpec and ImplementationState are external data structures that the agent
    interacts with through FunctionTools.
    """

    def __init__(
        self,
        llm: str,
        max_iterations: int = 500,
        user_token: str = "",
        skip_registration: bool = False,
    ):
        self.system_prompt = render_exploration_prompt()
        self.max_iterations = max_iterations
        self.llm_model = llm
        self.user_token = user_token

        self.task_spec = TaskSpec()
        self.impl_state = ImplementationState()
        self.skip_registration = skip_registration

        self.chat_client = self._create_chat_client(llm)
        self._mcp_tool = None

    # ------------------------------------------------------------------
    # Async context manager & cleanup
    # ------------------------------------------------------------------

    async def __aenter__(self):
        """Enter async context manager."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Exit async context manager — cleanup MCP connections."""
        await self.close()

    async def close(self):
        """Close MCP connections and cleanup resources."""
        if self._mcp_tool is not None:
            try:
                await self._mcp_tool.close()
                LOGGER.debug("MCP tool closed successfully")
            except Exception as e:
                LOGGER.warning(f"Error closing MCP tool: {e}")
            finally:
                self._mcp_tool = None

    # ------------------------------------------------------------------
    # Chat client
    # ------------------------------------------------------------------

    def _create_chat_client(
        self,
        deployment_name: str,
        azure_endpoint: Optional[str] = None,
        aoai_scope: Optional[str] = None,
        api_version: Optional[str] = None,
    ) -> AzureOpenAIChatClient:
        """Create AzureOpenAIChatClient with Entra ID authentication."""
        endpoint = azure_endpoint or os.getenv("AZURE_OPENAI_ENDPOINT")
        if not endpoint:
            raise EnvironmentError("Environmental variable AZURE_OPENAI_ENDPOINT not found.")

        scope = aoai_scope or os.getenv("AOAI_SCOPE")
        if not scope:
            raise EnvironmentError("Environmental variable AOAI_SCOPE not found.")

        api_version = api_version or os.getenv("API_VERSION")
        if not api_version:
            raise EnvironmentError("Environmental variable API_VERSION not found.")

        token_provider = create_entra_token_provider(scope)

        return AzureOpenAIChatClient(
            endpoint=endpoint,
            api_version=api_version,
            deployment_name=deployment_name,
            credential=token_provider,
            function_invocation_configuration={
                "include_detailed_errors": True,
            },
        )

    # ------------------------------------------------------------------
    # Workflow execution
    # ------------------------------------------------------------------

    async def go(self, prompt: str, input_handler: Callable | None = None) -> Message:
        """
        Execute the agent with the given prompt.

        When the agent needs help/clarification, it yields a WorkflowEvent
        requesting info.  This method handles the interactive loop: collecting
        user input and resuming the workflow until it produces a final output.

        Args:
            prompt: The user's query
            input_handler: Optional async callable that takes a question string
                and returns user input. If None, defaults to console input.

        Returns:
            The last message as a Message object
        """
        USER_LOGGER.info(f"Starting ToolMaker workflow with prompt: {prompt}")

        if input_handler is None:

            async def default_input_handler(question: str, context: str = "") -> str:
                if context:
                    print(f"\n📋 Context: {context}")
                print(f"\n🤖 Agent needs help: {question}")
                return await asyncio.to_thread(input, "Your response: ")

            input_handler = default_input_handler

        workflow = self._build_workflow()
        result = await workflow.run(prompt)

        while True:
            outputs = result.get_outputs()
            if outputs:
                break

            pending = result.get_request_info_events()
            if not pending:
                break

            responses = {}
            for req_event in pending:
                request_data = req_event.data
                question = getattr(request_data, "question", str(request_data))
                context = getattr(request_data, "context", "")
                user_input = await input_handler(question, context)
                responses[req_event.request_id] = user_input

            result = await workflow.run(responses=responses)

        if outputs:
            output = outputs[-1]
            if isinstance(output, SolutionResponse):
                return Message(role="assistant", text=output.solution)
            elif isinstance(output, HelpResponse):
                return Message(role="assistant", text=output.question)
            else:
                return Message(role="assistant", text=str(output))

        return Message(role="assistant", text="Workflow completed without final output")

    # ------------------------------------------------------------------
    # Workflow
    # ------------------------------------------------------------------

    def _build_workflow(self) -> "Workflow":
        """
        Build the four-phase ToolMaker workflow.

        Each phase has its own LLM executor with a phase-specific system prompt
        and toolset. Bridge executors connect phases. Phase 3 (User Decision)
        branches into reusable → registration or session-only → terminate.
        Phase 4 (Registration) can loop back to Phase 2 for revisions.

        When ``skip_registration`` is True (API caller path via toolmaker_tool.py),
        both Phase 3 and Phase 4 are skipped — the build bridge directly yields
        a SolutionResponse.
        """

        # ── Shared resources ──────────────────────────────────────────
        repo_tools = create_repo_tools()
        task_spec_tools = create_task_spec_tools(self.task_spec)
        codegen_tools = create_codegen_tools(self.task_spec)
        docker_tools = create_docker_tools(self.impl_state)
        registration_tools = create_registration_tools()

        # ── Phase 1: Exploration ──────────────────────────────────────
        exploration_tool_list: list = list(repo_tools) + list(task_spec_tools)

        exploration_llm = ToolMakerLLMExecutor(
            self.chat_client,
            render_exploration_prompt(),
            self.max_iterations,
            tools=exploration_tool_list,
            executor_id="exploration_llm",
        )
        exploration_help = ExplorationHelpHandlerExecutor(exploration_llm, self.task_spec)

        expl_to_build_bridge = ExplorationToBuildBridgeExecutor(self.task_spec, self.impl_state)

        # ── Phase 2: Build & Test ────────────────────────────────────
        build_tool_list: list = list(codegen_tools) + list(docker_tools) + list(repo_tools)

        build_llm = ToolMakerLLMExecutor(
            self.chat_client,
            render_implementation_prompt(),
            self.max_iterations,
            tools=build_tool_list,
            executor_id="build_llm",
        )
        build_help = HelpHandlerExecutor(llm_executor=build_llm)
        build_help.id = "build_help_handler"

        build_to_decision_bridge = BuildToDecisionBridgeExecutor(self.task_spec, self.impl_state)

        # ── Wire the graph ────────────────────────────────────────────
        builder = WorkflowBuilder(start_executor=exploration_llm)

        # Phase 1 edges
        builder.add_switch_case_edge_group(
            exploration_llm,
            [
                Case(
                    condition=lambda msg: isinstance(msg.response, SolutionResponse),
                    target=expl_to_build_bridge,
                ),
                Default(target=exploration_help),
            ],
        )
        builder.add_edge(exploration_help, exploration_llm)

        # Bridge: exploration → build & test
        builder.add_edge(expl_to_build_bridge, build_llm)

        # Phase 2 edges
        builder.add_switch_case_edge_group(
            build_llm,
            [
                Case(
                    condition=lambda msg: isinstance(msg.response, SolutionResponse),
                    target=build_to_decision_bridge,
                ),
                Default(target=build_help),
            ],
        )
        builder.add_edge(build_help, build_llm)

        # Bridge routes conditionally based on bridge._forward:
        #   True  → next phase (user decision or string terminal)
        #   False → back to build_llm (retry loop)
        bridge = build_to_decision_bridge  # capture for lambda closure

        if self.skip_registration:
            # API caller path: skip Phase 3 + 4 entirely.
            # Route bridge output to a terminal that converts str → SolutionResponse.
            string_terminal = StringToSolutionExecutor()
            builder.add_switch_case_edge_group(
                build_to_decision_bridge,
                [
                    Case(
                        condition=lambda _msg: bridge._forward,
                        target=string_terminal,
                    ),
                    Default(target=build_llm),
                ],
            )
        else:
            # ── Phase 3: User Decision ────────────────────────────────
            user_decision = UserDecisionExecutor(
                build_llm_executor=build_llm,
                task_spec=self.task_spec,
                impl_state=self.impl_state,
            )
            builder.add_switch_case_edge_group(
                build_to_decision_bridge,
                [
                    Case(
                        condition=lambda _msg: bridge._forward,
                        target=user_decision,
                    ),
                    Default(target=build_llm),
                ],
            )

            # ── Phase 4: Registration (only reached if user chose reusable) ──
            registration_tool_list: list = list(registration_tools)

            registration_llm = ToolMakerLLMExecutor(
                self.chat_client,
                render_registration_prompt(),
                self.max_iterations,
                tools=registration_tool_list,
                executor_id="registration_llm",
            )
            registration_help = RegistrationHelpHandlerExecutor(
                registration_llm_executor=registration_llm,
                build_llm_executor=build_llm,
                task_spec=self.task_spec,
                impl_state=self.impl_state,
            )

            # Terminal: safety fallback for SolutionResponse from registration_llm
            final_solution = SolutionHandlerExecutor()
            final_solution.id = "final_solution_handler"

            # User decision routing:
            # - "session-only" → ctx.yield_output() (no edge needed, terminates)
            # - "reusable" → sets persistence=REUSABLE, sends str to registration_llm
            # - "revision" → sends str to build_llm (loop back)
            #
            # We use a conditional edge group: check impl_state.persistence at
            # routing time (already set by the executor before send_message).
            impl_state = self.impl_state  # capture for lambda closure
            builder.add_switch_case_edge_group(
                user_decision,
                [
                    Case(
                        condition=lambda _msg: impl_state.persistence == ToolPersistence.REUSABLE,
                        target=registration_llm,
                    ),
                    Default(target=build_llm),
                ],
            )

            # Phase 4 edges
            builder.add_switch_case_edge_group(
                registration_llm,
                [
                    Case(
                        condition=lambda msg: isinstance(msg.response, SolutionResponse),
                        target=final_solution,
                    ),
                    Default(target=registration_help),
                ],
            )
            # Revision feedback from registration routes back to build
            builder.add_edge(registration_help, build_llm)

        return builder.build()
