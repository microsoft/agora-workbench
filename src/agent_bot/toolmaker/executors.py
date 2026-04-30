"""
Executors for the four-phase ToolMaker workflow.

Phase 1 — Exploration:     ToolMakerLLMExecutor (exploration_llm)
                            ExplorationHelpHandlerExecutor
Phase 2 — Build & Test:    ToolMakerLLMExecutor (build_llm)
                            (reuses core HelpHandlerExecutor)
Phase 3 — User Decision:   UserDecisionExecutor
                            (asks reusable vs one-time vs revision)
Phase 4 — Registration:    ToolMakerLLMExecutor (registration_llm)
                            RegistrationHelpHandlerExecutor

Bridge executors connect the phases:
  ExplorationToBuildBridgeExecutor  — exploration → build & test
  BuildToDecisionBridgeExecutor     — build & test → user decision
"""

import logging
from typing import Never, Optional, TYPE_CHECKING

from agent_framework import (
    Executor,
    WorkflowContext,
    handler,
    response_handler,
)

from agent_bot.agora.executors import BaseLLMExecutor, HelpRequest
from agent_bot.agora.response_models import (
    AgentResponse,
    HelpResponse,
    SolutionResponse,
)

if TYPE_CHECKING:
    from .models import TaskSpec, ImplementationState

LOGGER = logging.getLogger(__name__)
USER_LOGGER = logging.getLogger("user")
STATUS_LOGGER = logging.getLogger("status")


# ============================================================================
# LLM Executor (shared across all three stages)
# ============================================================================


class ToolMakerLLMExecutor(BaseLLMExecutor):
    """LLM Executor for each stage of the ToolMaker workflow.

    Unlike PlanThenExecuteLLMExecutor, this does NOT support dynamic tool loading
    (get_tool / BM25 search). The ToolMaker agent uses a fixed set of tools
    per stage: repo tools, codegen tools, docker tools, and registration tools.

    Each stage instantiates this class with a stage-specific system prompt,
    tool set, and executor ID.
    """

    def __init__(
        self,
        chat_client,
        system_prompt: str,
        max_iterations: int,
        tools: Optional[list] = None,
        executor_id: str = "toolmaker_llm",
    ):
        # Pass skill_paths=[] to disable auto-discovery of domain skill files
        super().__init__(chat_client, system_prompt, max_iterations, tools, skill_paths=[])
        self.id = executor_id

        # Although we don't use dynamic tool loading, BaseLLMExecutor expects these
        self.retrieved_tools: list = []


# ============================================================================
# Stage 1 — Exploration: Help Handler
# ============================================================================


class ExplorationHelpHandlerExecutor(Executor):
    """Help handler for the exploration stage.

    Displays the current task specification alongside the agent's question
    so the user can see what has been defined while providing feedback.
    """

    def __init__(self, llm_executor: ToolMakerLLMExecutor, task_spec: "TaskSpec"):
        super().__init__(id="exploration_help_handler")
        self.llm_executor = llm_executor
        self.task_spec = task_spec

    @handler
    async def handle_help(self, agent_response: AgentResponse, ctx: WorkflowContext[AgentResponse]) -> None:
        action = agent_response.response
        if not isinstance(action, HelpResponse):
            return

        USER_LOGGER.info(f"[Exploration] Agent asks: {action.question}")
        STATUS_LOGGER.info("PHASE: exploration — requesting user feedback")

        spec_view = self.task_spec.view()
        context = f"{agent_response.explanation}\n\n{spec_view}"

        help_request = HelpRequest(question=action.question, context=context)
        await ctx.request_info(help_request, str)

    @response_handler
    async def handle_help_response(self, _: HelpRequest, response: str, ctx: WorkflowContext[str]) -> None:
        USER_LOGGER.info(f"[Exploration] User feedback: {response}")
        await ctx.send_message(f"User provided feedback: {response}")


# ============================================================================
# Stage 1 → Stage 2 Bridge
# ============================================================================


class ExplorationToBuildBridgeExecutor(Executor):
    """Transitions from exploration to the build & test phase.

    Passes the finalized TaskSpec as context for the build phase.
    """

    def __init__(self, task_spec: "TaskSpec", impl_state: "ImplementationState"):
        super().__init__(id="exploration_to_build_bridge")
        self.task_spec = task_spec
        self.impl_state = impl_state

    @handler
    async def handle_exploration_done(self, agent_response: AgentResponse, ctx: WorkflowContext[str]) -> None:
        STATUS_LOGGER.info("PHASE: transitioning from exploration to build & test")
        USER_LOGGER.info("Task specification finalized — beginning build & test phase")

        spec_view = self.task_spec.view()
        kickoff = (
            "The task specification has been finalized. Begin implementing the domain server.\n\n"
            f"{spec_view}\n\n"
            "Start by reading the example domain files to understand the expected structure, "
            "then generate all required files. After generating, build, test, and iterate.\n\n"
            f"Python function signature: {self.task_spec.python_signature()}\n"
            f"Install commands: {self.task_spec.install_commands or '(to be determined from requirements)'}\n"
            f"Implementation plan: {self.task_spec.implementation_plan or '(follow standard CodeExecutionServer pattern)'}"
        )
        await ctx.send_message(kickoff)


# Keep the old name as an alias for backward compatibility
ExplorationToImplementationBridgeExecutor = ExplorationToBuildBridgeExecutor


# ============================================================================
# Stage 2 → Stage 3 Bridge
# ============================================================================


_RETRY_KEYWORDS = frozenset(
    {
        "retry",
        "try again",
        "fix",
        "redo",
        "keep trying",
        "continue fixing",
        "try harder",
    }
)


class BuildToDecisionBridgeExecutor(Executor):
    """Validates build results and transitions to the user decision phase.

    When the build LLM emits a SolutionResponse, this bridge checks whether
    tests actually passed.  If they did, it forwards a summary to the next
    phase (UserDecisionExecutor or StringToSolutionExecutor).  If they didn't,
    it asks the user whether to retry or proceed anyway — preventing the LLM
    from silently skipping past a failed build.

    Routing is controlled by ``self._forward``:
    - ``True``  → message routes to the next phase (Case edge)
    - ``False`` → message routes back to build_ctx (Default edge)
    """

    def __init__(self, task_spec: "TaskSpec", impl_state: "ImplementationState"):
        super().__init__(id="build_to_decision_bridge")
        self.task_spec = task_spec
        self.impl_state = impl_state
        self._forward: bool = False

    def _build_summary(self, solution_summary: str = "") -> str:
        spec_view = self.task_spec.view()
        impl_view = self.impl_state.view()
        return (
            f"Task Specification:\n{spec_view}\n\nImplementation Status:\n{impl_view}\n\nSummary:\n{solution_summary}"
        )

    @handler
    async def handle_build_done(self, agent_response: AgentResponse, ctx: WorkflowContext[str]) -> None:
        solution_summary = ""
        if isinstance(agent_response.response, SolutionResponse):
            solution_summary = agent_response.response.solution

        if self.impl_state.tests_passed:
            # ── Tests passed — proceed to next phase ──
            STATUS_LOGGER.info("PHASE: build passed — proceeding to user decision")
            USER_LOGGER.info("Build & test complete — all tests passed")
            self._forward = True
            await ctx.send_message(self._build_summary(solution_summary))
        else:
            # ── Tests haven't passed — ask the user what to do ──
            STATUS_LOGGER.info("PHASE: build incomplete — asking user whether to retry")
            USER_LOGGER.info("Build LLM declared done but tests haven't all passed")

            impl_view = self.impl_state.view()
            question = (
                "The build phase completed but not all tests have passed yet.\n\n"
                "Would you like to:\n"
                "  • Type 'retry' — Send the agent back to fix the issues and try again\n"
                "  • Type 'continue' — Proceed anyway with the current state"
            )
            context = f"{impl_view}\n\n{solution_summary}"
            help_request = HelpRequest(question=question, context=context)
            await ctx.request_info(help_request, str)

    @response_handler
    async def handle_retry_response(self, _: HelpRequest, response: str, ctx: WorkflowContext[str]) -> None:
        normalized = response.strip().lower().rstrip(".,!?")

        if normalized in _RETRY_KEYWORDS:
            # ── Retry: route back to build phase ──
            USER_LOGGER.info("[Bridge] User chose to retry — routing back to build")
            STATUS_LOGGER.info("PHASE: retrying build")
            self._forward = False

            correction = (
                "The user reviewed the build results and wants you to keep trying.\n\n"
                f"{self.impl_state.view()}\n\n"
                "Please fix the remaining issues, rebuild, re-test, and "
                "produce a SolutionResponse only when all tests pass."
            )
            await ctx.send_message(correction)
        else:
            # ── Continue/proceed anyway ──
            USER_LOGGER.info("[Bridge] User chose to proceed despite incomplete tests")
            STATUS_LOGGER.info("PHASE: proceeding to user decision despite incomplete tests")
            self._forward = True
            await ctx.send_message(self._build_summary())


# Keep the old name as an alias for backward compatibility
ImplementationToRegistrationBridgeExecutor = BuildToDecisionBridgeExecutor


# ============================================================================
# Stage 3 — User Decision: reusable vs session-only vs revision
# ============================================================================


_REUSABLE_KEYWORDS = frozenset(
    {
        "reusable",
        "save",
        "keep",
        "permanent",
        "register",
        "persist",
        "store",
    }
)

_ONE_TIME_KEYWORDS = frozenset(
    {
        "one-time",
        "one time",
        "onetime",
        "session",
        "temporary",
        "temp",
        "just use it",
        "no",
        "nah",
        "skip",
        "don't save",
        "dont save",
        "no thanks",
    }
)


class UserDecisionExecutor(Executor):
    """Asks the user whether to make the tool reusable or session-only.

    Three possible outcomes:
    - **Reusable**: sets persistence to REUSABLE, sends kickoff to registration.
    - **Session-only**: sets persistence to SESSION_ONLY, yields a
      SolutionResponse (workflow terminates, MCP server stays live).
    - **Revision feedback**: routes back to the build phase for edits.
    """

    def __init__(
        self,
        build_llm_executor: "ToolMakerLLMExecutor",
        task_spec: "TaskSpec",
        impl_state: "ImplementationState",
    ):
        super().__init__(id="user_decision")
        self.build_llm = build_llm_executor
        self.task_spec = task_spec
        self.impl_state = impl_state

    @handler
    async def handle_build_summary(self, summary: str, ctx: WorkflowContext) -> None:
        USER_LOGGER.info("[Decision] Tool built and tested — asking user about persistence")
        STATUS_LOGGER.info("PHASE: user decision — reusable or session-only")

        question = (
            f"Your tool '{self.task_spec.tool_name}' has been built and all tests passed!\n\n"
            "Would you like to:\n"
            "  • Type 'reusable' — Register this tool permanently so it's available in future sessions\n"
            "  • Type 'session' — Use it for this session only (no registration)\n"
            "  • Or provide feedback to revise the implementation"
        )
        context = f"{summary}"
        help_request = HelpRequest(question=question, context=context)
        await ctx.request_info(help_request, str)

    @response_handler
    async def handle_decision_response(
        self, original_request: HelpRequest, response: str, ctx: WorkflowContext[str, SolutionResponse]
    ) -> None:
        from .models import ToolPersistence

        normalized = response.strip().lower().rstrip(".,!?")

        if normalized in _REUSABLE_KEYWORDS:
            # ── Reusable: proceed to registration ──
            USER_LOGGER.info("[Decision] User chose reusable — proceeding to registration")
            STATUS_LOGGER.info("PHASE: user chose reusable tool")
            self.impl_state.persistence = ToolPersistence.REUSABLE

            spec_view = self.task_spec.view()
            impl_view = self.impl_state.view()
            kickoff = (
                "The user chose to make this tool reusable. "
                "Register the domain in AgoraAgentMAF config files and present results to the user.\n\n"
                f"Task Specification:\n{spec_view}\n\n"
                f"Implementation Status:\n{impl_view}\n\n"
                "Steps:\n"
                "1. Register the domain using register_domain\n"
                "2. Verify registration with view_registration_status\n"
                "3. Present everything to the user and ask for approval"
            )
            await ctx.send_message(kickoff)

        elif normalized in _ONE_TIME_KEYWORDS:
            # ── Session-only: yield output, workflow terminates ──
            USER_LOGGER.info("[Decision] User chose session-only — tool stays live but not registered")
            STATUS_LOGGER.info("PHASE: user chose session-only tool")
            self.impl_state.persistence = ToolPersistence.SESSION_ONLY

            spec_view = self.task_spec.view()
            impl_view = self.impl_state.view()
            solution = SolutionResponse(
                action="solution",
                solution=(
                    f"Tool '{self.task_spec.tool_name}' is ready for this session.\n\n"
                    f"The MCP server is running at {self.impl_state.server_url or 'localhost'}. "
                    f"It will remain available for the rest of this session but will not be "
                    f"registered for future use.\n\n{spec_view}\n\n{impl_view}"
                ),
                provenance="ToolMaker agent",
            )
            await ctx.yield_output(solution)

        else:
            # ── Revision feedback: route back to build phase ──
            USER_LOGGER.info(f"[Decision] User requested revisions: {response}")
            STATUS_LOGGER.info("PHASE: revision requested — returning to build & test")

            revision_prompt = (
                f"The user reviewed the results and requested revisions:\n\n"
                f'"{response}"\n\n'
                f"Make the necessary changes to the domain server code, "
                f"rebuild, re-test, and produce a SolutionResponse when done."
            )
            await ctx.send_message(revision_prompt)


# ============================================================================
# Stage 3 — Registration: Help Handler
# ============================================================================


class StringToSolutionExecutor(Executor):
    """Terminal executor that receives a str and yields it as a SolutionResponse.

    Used in the skip_registration path where the bridge outputs a str but the
    workflow needs to yield a SolutionResponse as final output.
    """

    def __init__(self):
        super().__init__(id="string_to_solution")

    @handler
    async def handle_string(self, message: str, ctx: WorkflowContext[Never, SolutionResponse]) -> None:
        solution = SolutionResponse(action="solution", solution=message)
        STATUS_LOGGER.info("PHASE: solution finalized (skip_registration)")
        USER_LOGGER.info(f"Solution: {message[:200]}")
        await ctx.yield_output(solution)


_ACCEPT_KEYWORDS = frozenset(
    {
        "accept",
        "accepted",
        "approve",
        "approved",
        "done",
        "looks good",
        "lgtm",
        "yes",
        "ok",
        "okay",
        "good",
        "great",
        "perfect",
        "fine",
        "ship it",
        "looks great",
        "that's good",
        "thats good",
    }
)


class RegistrationHelpHandlerExecutor(Executor):
    """Help handler for the registration phase (Phase 4).

    Displays results and handles accept/revise from the user.
    Accept → terminates the workflow.
    Revise → routes back to the build phase for edits.
    """

    def __init__(
        self,
        registration_llm_executor: "ToolMakerLLMExecutor",
        build_llm_executor: "ToolMakerLLMExecutor",
        task_spec: "TaskSpec",
        impl_state: "ImplementationState",
    ):
        super().__init__(id="registration_help_handler")
        self.registration_llm = registration_llm_executor
        self.build_llm = build_llm_executor
        self.task_spec = task_spec
        self.impl_state = impl_state

    @handler
    async def handle_help(self, agent_response: AgentResponse, ctx: WorkflowContext[AgentResponse]) -> None:
        action = agent_response.response
        if not isinstance(action, HelpResponse):
            return

        USER_LOGGER.info(f"[Registration] Agent presents: {action.question}")
        STATUS_LOGGER.info("PHASE: registration — awaiting user acceptance or revision")

        impl_view = self.impl_state.view()
        context = (
            f"{agent_response.explanation}\n\n"
            f"══ Implementation Status ══\n{impl_view}\n\n"
            "Type 'accept' to finalize, or provide feedback to request changes."
        )

        help_request = HelpRequest(question=action.question, context=context)
        await ctx.request_info(help_request, str)

    @response_handler
    async def handle_help_response(
        self, original_request: HelpRequest, response: str, ctx: WorkflowContext[str, SolutionResponse]
    ) -> None:
        normalized = response.strip().lower().rstrip(".,!?")

        if normalized in _ACCEPT_KEYWORDS:
            # ── User accepted → terminate the workflow ──
            USER_LOGGER.info("[Registration] User accepted results")
            STATUS_LOGGER.info("PHASE: results accepted — ToolMaker workflow complete")

            spec_view = self.task_spec.view()
            impl_view = self.impl_state.view()
            solution = SolutionResponse(
                action="solution",
                solution=(
                    f"ToolMaker workflow complete. Domain '{self.task_spec.domain_name}' "
                    f"has been created and registered.\n\n{spec_view}\n\n{impl_view}"
                ),
                provenance="ToolMaker agent",
            )
            await ctx.yield_output(solution)
        else:
            # ── User requested revisions → route back to build phase ──
            USER_LOGGER.info(f"[Registration] User requested revisions: {response}")
            STATUS_LOGGER.info("PHASE: revision requested — returning to build & test")

            revision_prompt = (
                f"The user reviewed the results and requested revisions:\n\n"
                f'"{response}"\n\n'
                f"Make the necessary changes to the domain server code, "
                f"rebuild, re-test, and produce a SolutionResponse when done."
            )
            await ctx.send_message(revision_prompt)
