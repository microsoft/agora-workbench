"""
PlanThenExecuteAgent — a three-stage agent that (1) builds an execution plan
collaboratively with the user, (2) executes the plan autonomously, and (3)
presents the results for user review with the option to request revisions.

When ``autopilot=True``, the agent skips all user interaction: it builds the
plan on its own, executes it without prompting the user for help, and
delivers the final results directly; any help handlers are auto-resolved
internally instead of blocking for user input.

The plan is an external data structure that the agent manipulates via tools
(view_plan, add_step, set_step_status, finalize_plan, etc.).  Each stage
uses a dedicated LLM executor with a stage-specific system prompt and toolset.

Workflow graph
──────────────

  Stage 1 — Planning
  ┌──────────────────────────────────────────────────────────────┐
  │  PlanningLLM ─[Help]─► PlanningHelp ─┐                      │
  │       ▲                               │                      │
  │       └───────────────────────────────┘                      │
  │              ─[Solution]─► PlanToExecBridge                  │
  └──────────────────────────┬───────────────────────────────────┘
                             ▼
  Stage 2 — Execution
  ┌──────────────────────────────────────────────────────────────┐
  │  ExecutionLLM ─[Help]─► ExecutionHelp ─┐                    │
  │       ▲                                 │                    │
  │       └─────────────────────────────────┘                    │
  │                ─[Solution]─► ExecToPresentBridge              │
  └────────────────────────────┬─────────────────────────────────┘
                               ▼
  Stage 3 — Presentation
  ┌──────────────────────────────────────────────────────────────┐
  │  PresentationLLM ─[Help]─► PresentationHelp                 │
  │                                │                             │
  │                          [accept] → yield_output (terminate) │
  │                          [revise] → ExecutionLLM             │
  │                ─[Solution]─► FinalSolution (yield_output)    │
  └──────────────────────────────────────────────────────────────┘

In autopilot mode, Help handlers auto-resolve instead of pausing for user
input, so the graph traversal never blocks.

Context compaction is handled automatically by MAF-native CompactionProvider
registered inside each stage's BaseLLMExecutor.
"""

import asyncio
import logging
import os
from pathlib import Path
from typing import Callable, Optional, Type, TYPE_CHECKING

import planning as _planning_pkg
from agent_framework import (
    Message,
    WorkflowBuilder,
    Case,
    Default,
)
from agent_framework.azure import AzureOpenAIChatClient

from auth import create_entra_token_provider
from data_lake.tools.data_lake import create_data_lake_search_tool, is_data_lake_configured
from tools.mcp import create_mcp_tools, get_mcp_registry
from tools.search import (
    BM25ToolSearchBackend,
    create_search_tools_function,
    create_query_state_graph_function,
    create_load_skill_function,
)
from .executors import (
    BaseLLMExecutor,
    SolutionHandlerExecutor,
    HelpHandlerExecutor,
    PlanningHelpHandlerExecutor,
    PlanToExecutionBridgeExecutor,
    ExecToPresentationBridgeExecutor,
    PresentationHelpHandlerExecutor,
)
from .response_models import SolutionResponse, HelpResponse
from .plan import Plan
from .plan_tools import create_plan_tools, create_plan_view_tool
from .prompts.renderer import (
    render_planning_prompt,
    render_execution_prompt,
    render_presentation_prompt,
)

if TYPE_CHECKING:
    from agent_framework import Workflow
    from tools import ToolSearchBackend

LOGGER = logging.getLogger(__name__)

# Top-level (non-domain) skills advertised via SkillsProvider.
# Domain skills are discovered on demand via query_state_graph + load_skill.
_PLANNING_SKILLS_DIR = Path(_planning_pkg.__file__).resolve().parent / "skills"


def _advertised_skill_paths() -> list[str]:
    """Skill paths for SkillsProvider — only non-domain skills (e.g. planning)."""
    paths: list[str] = []
    if _PLANNING_SKILLS_DIR.is_dir():
        paths.append(str(_PLANNING_SKILLS_DIR))
    return paths


class PlanThenExecuteAgent:
    """
    Three-stage agent: plan → execute → present.

    Stage 1 (Planning):     Build a plan collaboratively with the user.
    Stage 2 (Execution):    Execute the finalized plan autonomously.
    Stage 3 (Presentation): Present results; user can accept or request revisions.

    The plan is stored in an external ``Plan`` object and the agent interacts with
    it only through plan management FunctionTools.

    Tool setup (MCP servers, search_tools, data lake) happens at the agent level
    and is passed into each stage's executor.
    """

    def __init__(
        self,
        llm: str,
        max_iterations: int = 500,
        user_token: str = "",
        search_backend: Optional[Type["ToolSearchBackend"]] = None,
        context_providers: Optional[list] = None,
        middleware: Optional[list] = None,
        autopilot: bool = False,
    ):
        self.plan = Plan()
        self._search_backend_cls = search_backend
        self._user_token = user_token
        self.autopilot = autopilot

        # Extra context providers passed through to each stage's MAF Agent
        self._context_providers: list = context_providers or []

        # Extra middleware passed through to each stage's MAF Agent
        self._middleware: list = middleware or []

        # Use the planning prompt as the base system_prompt
        self.system_prompt = render_planning_prompt(autopilot=autopilot)
        self.max_iterations = max_iterations
        self.llm_model = llm
        self.user_token = user_token

        # Initialize MAF Agent with appropriate client
        self.chat_client = self._create_chat_client(llm)

    async def __aenter__(self):
        """Enter async context manager."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Exit async context manager - cleanup resources."""
        await self.close()

    async def close(self):
        """Close MCP connections and clean up registry resources."""
        try:
            from tools.mcp.mcp_server_registry import get_mcp_registry, reset_mcp_registry

            await get_mcp_registry().aclose()
            reset_mcp_registry()
            LOGGER.debug("MCP registry closed and reset successfully")
        except Exception as e:
            LOGGER.warning(f"Error closing MCP registry: {e}")

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
        )

    # ------------------------------------------------------------------
    # Tool setup (agent-level)
    # ------------------------------------------------------------------

    def _build_mcp_and_search_tools(self) -> list:
        """Build MCP server tools and search_tools FunctionTool.

        Returns:
            List of tools shared across all workflow stages.
        """
        tools: list = []

        # MCP tools for each registered server
        mcp_registry = get_mcp_registry()
        for server_name in mcp_registry.list_servers():
            mcp_tool = create_mcp_tools(server_name)
            if mcp_tool is not None:
                tools.append(mcp_tool)
                LOGGER.info("Added MCP tools for server '%s'", server_name)

        # search_tools FunctionTool
        if self._search_backend_cls is None:
            search_backend = BM25ToolSearchBackend()
        else:
            search_backend = self._search_backend_cls(user_token=self._user_token)

        search_tools_fn = create_search_tools_function(search_backend)
        tools.append(search_tools_fn)
        LOGGER.info("Created search_tools FunctionTool for tool catalog search")

        return tools

    # ------------------------------------------------------------------
    # Workflow
    # ------------------------------------------------------------------

    async def _build_workflow(self) -> "Workflow":
        """
        Build the three-stage plan-then-execute workflow.

        Tools (MCP servers, search_tools, data lake) are assembled at the
        agent level and passed into each stage's BaseLLMExecutor.  Each stage
        has its own system prompt, plan tools, and executor ID.
        """

        # ── Agent-level tools (shared across stages) ─────────────────
        shared_tools = self._build_mcp_and_search_tools()

        # query_state_graph FunctionTool — lazily discovers tools from
        # MCP servers on first query so domain meta-tools are available
        state_graph_fn = create_query_state_graph_function()
        shared_tools.append(state_graph_fn)
        LOGGER.info("Created query_state_graph FunctionTool for workflow exploration")

        # load_skill FunctionTool — reads full SKILL.md content by name,
        # replacing the SkillsProvider context-injection approach
        load_skill_fn = create_load_skill_function()
        shared_tools.append(load_skill_fn)
        LOGGER.info("Created load_skill FunctionTool for on-demand skill loading")

        data_lake_tool = None
        if is_data_lake_configured():
            data_lake_tool = await create_data_lake_search_tool(user_token=self.user_token)
            LOGGER.info("Created search_data_lake_catalog tool for DataLake artifact discovery")

        # ── Shared plan resources ─────────────────────────────────────
        plan_tools = create_plan_tools(self.plan)
        plan_view_tool = create_plan_view_tool(self.plan)

        # ── Stage 1: Planning ─────────────────────────────────────────
        planning_tools: list = list(shared_tools) + list(plan_tools)
        if data_lake_tool:
            planning_tools.append(data_lake_tool)

        planning_llm = BaseLLMExecutor(
            self.chat_client,
            render_planning_prompt(autopilot=self.autopilot),
            self.max_iterations,
            tools=planning_tools,
            skill_paths=_advertised_skill_paths(),
            executor_id="planning_llm",
            context_providers=self._context_providers,
            middleware=self._middleware,
        )
        planning_help = PlanningHelpHandlerExecutor(
            planning_llm,
            self.plan,
            autopilot=self.autopilot,
        )

        plan_to_exec_bridge = PlanToExecutionBridgeExecutor(self.plan)

        # ── Stage 2: Execution ────────────────────────────────────────
        execution_tools: list = list(shared_tools) + list(plan_tools)
        if data_lake_tool:
            execution_tools.append(data_lake_tool)

        execution_llm = BaseLLMExecutor(
            self.chat_client,
            render_execution_prompt(autopilot=self.autopilot),
            self.max_iterations,
            tools=execution_tools,
            skill_paths=_advertised_skill_paths(),
            executor_id="execution_llm",
            context_providers=self._context_providers,
            middleware=self._middleware,
        )
        execution_help = HelpHandlerExecutor(
            llm_executor=execution_llm,
            autopilot=self.autopilot,
        )
        execution_help.id = "execution_help_handler"

        exec_to_present_bridge = ExecToPresentationBridgeExecutor(self.plan)

        # ── Stage 3: Presentation ─────────────────────────────────────
        presentation_tools: list = [plan_view_tool]

        presentation_llm = BaseLLMExecutor(
            self.chat_client,
            render_presentation_prompt(autopilot=self.autopilot),
            self.max_iterations,
            tools=presentation_tools,
            skill_paths=_advertised_skill_paths(),
            executor_id="presentation_llm",
            context_providers=self._context_providers,
            middleware=self._middleware,
        )
        presentation_help = PresentationHelpHandlerExecutor(
            presentation_llm_executor=presentation_llm,
            execution_llm_executor=execution_llm,
            plan=self.plan,
            autopilot=self.autopilot,
        )

        # Terminal: handles SolutionResponse from presentation_llm as a
        # safety fallback (the primary termination path is through
        # PresentationHelpHandlerExecutor.yield_output).
        final_solution = SolutionHandlerExecutor()
        final_solution.id = "final_solution_handler"

        # ── Wire the graph ────────────────────────────────────────────
        builder = WorkflowBuilder(start_executor=planning_llm)

        # Stage 1 edges
        builder.add_switch_case_edge_group(
            planning_llm,
            [
                Case(
                    condition=lambda msg: isinstance(msg.response, SolutionResponse),
                    target=plan_to_exec_bridge,
                ),
                Default(target=planning_help),
            ],
        )
        builder.add_edge(planning_help, planning_llm)

        # Bridge: planning → execution
        builder.add_edge(plan_to_exec_bridge, execution_llm)

        # Stage 2 edges
        builder.add_switch_case_edge_group(
            execution_llm,
            [
                Case(
                    condition=lambda msg: isinstance(msg.response, SolutionResponse),
                    target=exec_to_present_bridge,
                ),
                Default(target=execution_help),
            ],
        )
        builder.add_edge(execution_help, execution_llm)

        # Bridge: execution → presentation
        builder.add_edge(exec_to_present_bridge, presentation_llm)

        # Stage 3 edges
        builder.add_switch_case_edge_group(
            presentation_llm,
            [
                Case(
                    condition=lambda msg: isinstance(msg.response, SolutionResponse),
                    target=final_solution,
                ),
                Default(target=presentation_help),
            ],
        )
        # Revision feedback from presentation routes directly back to execution
        builder.add_edge(presentation_help, execution_llm)

        return builder.build()

    async def _run_workflow(self, prompt: str, input_handler: Callable) -> Message:
        """
        Run a single workflow pass: build workflow, execute until solution or exhaustion.

        Handles the help-request loop internally: when the agent requests user
        clarification, it collects input via *input_handler* and resumes.
        In autopilot mode, help requests are auto-resolved without user input.

        Returns the final Message for this pass.
        """
        workflow = await self._build_workflow()
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
                if self.autopilot:
                    # Safety net: auto-resolve any help requests that slip
                    # through despite executor-level autopilot handling
                    LOGGER.info(
                        "[Autopilot] Auto-resolving escaped request_info event %s",
                        req_event.request_id,
                    )
                    responses[req_event.request_id] = "Use your best judgment and proceed autonomously."
                else:
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

    async def run(self, prompt: str, input_handler: Callable | None = None) -> str:
        """
        Send a message and get a response.

        Args:
            prompt: The user's message
            input_handler: Optional async callable ``(question, context) -> str``
                used when the agent requests mid-workflow clarification.
                Defaults to console input.

        Returns:
            The agent's response text.
        """
        if input_handler is None:
            input_handler = self._default_input_handler

        result = await self._run_workflow(prompt, input_handler)
        return result.text

    async def go(self, prompt: str, input_handler: Callable | None = None) -> Message:
        """
        Execute the agent and return the raw Message object.

        Prefer :meth:`run` for new code.  This method is kept for backward
        compatibility.
        """
        if input_handler is None:
            input_handler = self._default_input_handler

        return await self._run_workflow(prompt, input_handler)

    @staticmethod
    async def _default_input_handler(question: str, context: str = "") -> str:
        """Default console-based input handler for help requests."""
        if context:
            print(f"\n📋 Context: {context}")
        print(f"\n🤖 Agent needs help: {question}")
        return await asyncio.to_thread(input, "Your response: ")
