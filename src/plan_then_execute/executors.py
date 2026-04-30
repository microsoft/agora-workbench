"""
Executors for the three-stage plan-then-execute workflow.

Contains both the base executors (BaseLLMExecutor, SolutionHandlerExecutor,
HelpHandlerExecutor) and the stage-specific executors.

Stage 1 — Planning:   BaseLLMExecutor (planning_llm)
                       PlanningHelpHandlerExecutor
Stage 2 — Execution:  BaseLLMExecutor (execution_llm)
                       (reuses HelpHandlerExecutor)
Stage 3 — Presentation: BaseLLMExecutor (presentation_llm)
                          PresentationHelpHandlerExecutor

Bridge executors connect the stages:
  PlanToExecutionBridgeExecutor   — planning  → execution
  ExecToPresentationBridgeExecutor — execution → presentation
"""

import httpx
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Never, TYPE_CHECKING

from agent_framework import (
    Agent,
    CharacterEstimatorTokenizer,
    CompactionProvider,
    InMemoryHistoryProvider,
    Executor,
    MCPStreamableHTTPTool,
    Message,
    SkillsProvider,
    SlidingWindowStrategy,
    SummarizationStrategy,
    TokenBudgetComposedStrategy,
    WorkflowContext,
    handler,
    response_handler,
)

from compaction import SkillAwareToolCompactionStrategy

from .response_models import (
    AgentResponse,
    HelpResponse,
    SolutionResponse,
)

if TYPE_CHECKING:
    from .plan import Plan

LOGGER = logging.getLogger(__name__)
USER_LOGGER = logging.getLogger("user")
STATUS_LOGGER = logging.getLogger("status")


# ============================================================================
# Request Types
# ============================================================================


@dataclass
class HelpRequest:
    """Request for user input in response to agent's help question."""

    question: str
    context: str = ""  # Explanation/context about why help is needed


# ============================================================================
# Base Workflow Executors
# ============================================================================


# Default skills directory: domains/*/skills under the project root
_DOMAINS_DIR = Path(__file__).resolve().parent.parent.parent / "domains"
_PLANNING_SKILLS_DIR = Path(__file__).resolve().parent.parent.parent / "planning" / "skills"


def _discover_skill_paths(domains_dir: Path = _DOMAINS_DIR) -> list[str]:
    """Collect all ``skills/`` directories under each domain subfolder,
    plus the standalone planning package skills.

    Returns a list of absolute paths like ``domains/powergrid/skills/``.
    ``SkillsProvider`` will search each for ``SKILL.md`` files up to
    2 levels deep, which covers structures like::

        domains / powergrid / skills / acopf / SKILL.md
        domains / powergrid / skills / powerflow / SKILL.md
        planning / skills / SKILL.md
    """
    paths: list[str] = []
    if domains_dir.is_dir():
        for child in sorted(domains_dir.iterdir()):
            skills_dir = child / "skills"
            if skills_dir.is_dir():
                paths.append(str(skills_dir))
    if _PLANNING_SKILLS_DIR.is_dir():
        paths.append(str(_PLANNING_SKILLS_DIR))
    return paths


class BaseLLMExecutor(Executor):
    """Base executor that calls the LLM with pre-built tools and returns structured AgentResponse.

    Provides the core LLM interaction loop: message building, agent invocation,
    response parsing with retry, and workflow message routing.

    Tool setup is handled at the agent level — all tools are passed in at
    construction and available from the first iteration.
    """

    def __init__(
        self,
        chat_client,
        system_prompt: str,
        max_iterations: int,
        tools: Optional[list] = None,
        skill_paths: Optional[list[str]] = None,
        executor_id: str = "llm_executor",
        context_providers: Optional[list] = None,
        middleware: Optional[list] = None,
    ):
        super().__init__(id=executor_id)
        self.chat_client = chat_client
        self.system_prompt = system_prompt
        self.max_iterations = max_iterations

        self.base_tools = tools or []  # Agent-level tools (FunctionTool objects)

        # Skill paths for FileAgentSkillsProvider (auto-discover from domains/ if not provided)
        self._skill_paths = skill_paths if skill_paths is not None else _discover_skill_paths()

        # Extra context providers supplied by the caller (e.g. DecisionLogContextProvider)
        self._extra_context_providers: list = context_providers or []

        # Extra middleware supplied by the caller (e.g. DecisionLogChatMiddleware)
        self._extra_middleware: list = middleware or []

        self._agent: Optional[Agent] = None
        self._thread = None
        self.iteration = 0

    # ------------------------------------------------------------------
    # Agent initialisation helpers
    # ------------------------------------------------------------------

    def _init_agent(self) -> None:
        """Lazily create the MAF Agent on first invocation."""
        if self._agent is None:
            # Build context providers (skills, etc.)
            context_providers = []
            if self._skill_paths:
                skills_provider = SkillsProvider(self._skill_paths)
                context_providers.append(skills_provider)
                LOGGER.info("Initialized SkillsProvider with paths: %s", self._skill_paths)

            # MAF-native history management and context compaction
            history_provider = InMemoryHistoryProvider(skip_excluded=True)
            tokenizer = CharacterEstimatorTokenizer()
            pipeline = TokenBudgetComposedStrategy(
                token_budget=20_000,
                tokenizer=tokenizer,
                strategies=[
                    SkillAwareToolCompactionStrategy(keep_last_tool_call_groups=1),
                    SummarizationStrategy(client=self.chat_client, target_count=10, threshold=11),
                    SlidingWindowStrategy(keep_last_groups=20),
                ],
            )
            compaction_provider = CompactionProvider(
                before_strategy=pipeline,
                history_source_id=history_provider.source_id,
            )
            context_providers.extend([history_provider, compaction_provider])

            # Append any caller-supplied providers (e.g. DecisionLogContextProvider)
            context_providers.extend(self._extra_context_providers)

            self._agent = Agent(
                client=self.chat_client,
                name="agora_agent",
                instructions=self.system_prompt,
                tools=self.base_tools,
                context_providers=context_providers,
                middleware=self._extra_middleware or None,
            )
            self._thread = self._agent.create_session()

    # ------------------------------------------------------------------
    # MCP tool validation
    # ------------------------------------------------------------------

    async def _validate_mcp_tools(self) -> None:
        """Remove MCP tools whose servers are unreachable before ``run()``.

        The agent framework enters ``MCPStreamableHTTPTool`` instances as async
        context managers during ``Agent.run()``.  If the MCP server is down the
        anyio/MCP stack raises ``CancelledError`` (a ``BaseException``) that
        escapes the framework's ``except Exception`` handlers and crashes the
        agent.  Pre-checking with a lightweight health request prevents this.
        """
        surviving: list = []
        for tool in self.base_tools:
            if isinstance(tool, MCPStreamableHTTPTool) and not tool.is_connected:
                base_url = tool.url.rsplit("/mcp", 1)[0]
                reachable = False
                try:
                    async with httpx.AsyncClient(
                        timeout=httpx.Timeout(5.0),
                    ) as client:
                        resp = await client.get(f"{base_url}/health")
                        reachable = resp.status_code == 200
                except Exception:
                    LOGGER.debug("Health check failed for '%s' at %s", tool.name, base_url, exc_info=True)
                if reachable:
                    surviving.append(tool)
                else:
                    LOGGER.warning(
                        "MCP server for tool '%s' is unreachable at %s — removing from tool list",
                        tool.name,
                        tool.url,
                    )
                    try:
                        await tool.close()
                    except Exception:
                        LOGGER.debug("Error closing tool '%s'", tool.name, exc_info=True)
            else:
                surviving.append(tool)
        self.base_tools[:] = surviving

    # ------------------------------------------------------------------
    # Core execution pipeline
    # ------------------------------------------------------------------

    @handler
    async def process_prompt(self, prompt: str, ctx: WorkflowContext[AgentResponse]) -> None:
        """Process initial user prompt or observation and get LLM response."""
        self.iteration += 1

        USER_LOGGER.info(f"LLM processing iteration {self.iteration}")
        STATUS_LOGGER.info("PHASE: prompting LLM")

        if self.iteration > self.max_iterations:
            LOGGER.warning(f"Max iterations reached ({self.max_iterations})")
            return

        self._init_agent()
        await self._execute_and_respond(prompt, ctx)

    async def _execute_and_respond(self, prompt: str, ctx: WorkflowContext[AgentResponse]) -> None:
        """Run the LLM once, parse the response, and send it onward.

        Passes only the latest user message; InMemoryHistoryProvider and
        CompactionProvider manage accumulated history automatically.

        Subclasses override this to wrap the call with rerun loops, dynamic
        tool injection, etc.
        """
        LOGGER.debug("Sending latest message to LLM")

        # Remove MCP tools whose servers went away between loading
        # and this rerun (prevents CancelledError crashes in the framework).
        await self._validate_mcp_tools()

        response = await self._agent.run(
            messages=[Message(role="user", text=prompt)],
            session=self._thread,
            client_kwargs={"response_format": AgentResponse},
        )

        await self._handle_response(response, ctx)

    async def _handle_response(self, response, ctx: WorkflowContext[AgentResponse]) -> None:
        """Extract the last message, parse as AgentResponse, and send to workflow."""
        USER_LOGGER.info("Received response from LLM")
        STATUS_LOGGER.info("PHASE: received LLM response")

        # response.text concatenates ALL messages (including pre-tool-call text),
        # which creates invalid JSON like "Let me run code... {json}".
        # We want only the final structured response.
        if response.messages:
            last_message_text = response.messages[-1].text
        else:
            last_message_text = response.text

        LOGGER.debug(f"Parsing response from last message (length={len(last_message_text)})")

        agent_response = await self._parse_response_with_retry(last_message_text)

        if agent_response.status:
            STATUS_LOGGER.info(f"STATUS: {agent_response.status}")

        await ctx.send_message(agent_response)

    async def _parse_response_with_retry(self, response_text: str, max_retries: int = 2) -> AgentResponse:
        """
        Parse LLM response as JSON with retry mechanism.

        If parsing fails (common after tool calls), asks the LLM to reformat
        its response as valid JSON matching the AgentResponse schema.

        Args:
            response_text: The raw text from the LLM
            max_retries: Maximum number of retry attempts

        Returns:
            Parsed AgentResponse
        """

        # First attempt - delegate parsing/cleanup to AgentResponse validators
        try:
            # Use model_validate so AgentResponse's @model_validator(mode="before")
            # can handle markdown-wrapped JSON or other string inputs.
            return AgentResponse.model_validate(response_text)
        except Exception as first_error:
            LOGGER.warning(f"Failed to parse structured response (will retry): {first_error}")

        # Retry loop - ask LLM to reformat
        for attempt in range(max_retries):
            LOGGER.info(f"Retry attempt {attempt + 1}/{max_retries}: Asking LLM to reformat response as JSON")

            reformat_prompt = f"""Your previous response was not valid JSON. Please reformat it as a valid AgentResponse JSON object.

Your previous response was:
{response_text[:2000]}

IMPORTANT: 
- Return ONLY raw JSON. Do NOT wrap in markdown code blocks.
- There is NO "execute" or "retrieval" action type. To run code, make a TOOL CALL to the appropriate function (e.g., execute_powergrid_code). To discover tools, call `search_tools`.
- ONLY use "solution" if the task is FULLY COMPLETE and you have actual results. Do NOT use "solution" to describe what you WILL do.
- ONLY use "help" if you GENUINELY need user clarification (missing parameters, ambiguous requirements). Do NOT use "help" to announce what you're about to do - just do it via tool calls.

- Use one of these EXACT schemas:

For a SOLUTION (final answer - ONLY when task is DONE with actual results):
{{
  "explanation": "your reasoning",
  "response": {{"action": "solution", "solution": "your final answer with actual results here", "provenance": "optional: what tools/data used"}},
  "status": "optional status message"
}}

For asking for HELP/clarification (ONLY when genuinely blocked by missing info):
{{
  "explanation": "your reasoning",
  "response": {{"action": "help", "question": "specific question about missing parameter or ambiguous requirement"}},
  "status": "optional status message"
}}

CRITICAL: 
- Use the EXACT field names shown above (solution, question - NOT result, answer, etc.)
- "action" must be one of: "solution", "help" (NOT "execute" or "retrieval")
- Do NOT return a "solution" that describes future intentions - that's not a solution!"""

            try:
                # Make a lightweight call to reformat
                reformat_response = await self._agent.run(
                    messages=[Message(role="user", text=reformat_prompt)],
                    session=self._thread,
                    client_kwargs={"response_format": AgentResponse},
                )

                # Try to parse the reformatted response
                # model_validate handles markdown stripping via the @model_validator
                return AgentResponse.model_validate(reformat_response.text)
            except Exception as retry_error:
                LOGGER.warning(f"Retry {attempt + 1} failed: {retry_error}")
                continue

        # All retries failed - fall back to HelpResponse
        LOGGER.error(f"All {max_retries} retries failed. Falling back to HelpResponse.")

        # Try to extract a meaningful question from the response text
        # Look for common patterns that might indicate what the LLM was asking
        fallback_question = (
            "I encountered an issue processing your request. Could you please clarify or rephrase what you need?"
        )

        # Try to find a JSON question field in the raw text
        question_match = re.search(r'"question"\s*:\s*"([^"]+)"', response_text)
        if question_match:
            fallback_question = question_match.group(1)
        else:
            # Try to find text after common help indicators
            help_match = re.search(r"(?:Can you|Could you|Please|What is|Where is)[^.?!]*[.?!]", response_text)
            if help_match:
                fallback_question = help_match.group(0)

        return AgentResponse(
            explanation="The LLM returned an unstructured response that could not be parsed as JSON after multiple retries.",
            response=HelpResponse(
                action="help",
                question=fallback_question,
            ),
            status="Failed to parse LLM response",
        )


class SolutionHandlerExecutor(Executor):
    """Executor that handles final solution.

    Receives: AgentResponse (with SolutionResponse action)
    Action: Yields solution as workflow output (terminal node)
    """

    def __init__(self):
        super().__init__(id="solution_handler")

    @handler
    async def handle_solution(
        self, agent_response: AgentResponse, ctx: WorkflowContext[Never, SolutionResponse]
    ) -> None:
        """Handle final solution - yield as workflow output.

        This is a terminal node that ends the workflow execution.
        """
        action = agent_response.response
        if not isinstance(action, SolutionResponse):
            return

        STATUS_LOGGER.info("PHASE: solution finalized")
        USER_LOGGER.info(f"Solution: {action.solution}")
        await ctx.yield_output(action)


class HelpHandlerExecutor(Executor):
    """Executor that handles help requests by prompting user for input.

    Receives: AgentResponse (with HelpResponse action)
    Action: Requests user input via ctx.request_info, waits for response
    Sends: observation string (user's response to help question)

    Uses MAF's request_info pattern: emits HelpRequest, waits for string response.

    In autopilot mode, auto-resolves help requests instead of asking the user.
    """

    def __init__(self, llm_executor: "BaseLLMExecutor", *, autopilot: bool = False):
        super().__init__(id="help_handler")
        self.llm_executor = llm_executor
        self.autopilot = autopilot

    @handler
    async def handle_help(self, agent_response: AgentResponse, ctx: WorkflowContext[str, Never]) -> None:
        """Handle help request - emit request for user input.

        Uses ctx.request_info to pause workflow and request user input.
        The response will be handled by handle_help_response.

        In autopilot mode, sends an auto-resolved observation instead.
        """
        action = agent_response.response
        if not isinstance(action, HelpResponse):
            return

        if self.autopilot:
            LOGGER.info("[Autopilot] Auto-resolving help request: %s", action.question)
            STATUS_LOGGER.info("PHASE: autopilot — auto-resolving help request")
            synthetic_help = (
                "In autopilot mode, no explicit user clarification is available. "
                "Proceed using your best judgment based on the existing context and the "
                f"question: {action.question!r}. Do not ask for further clarification."
            )
            await ctx.send_message(f"User provided help input (autopilot): {synthetic_help}")
            return

        USER_LOGGER.info(f"Agent requested help: {action.question}")
        STATUS_LOGGER.info("PHASE: requesting user input for help")

        # Request user input using MAF's request_info pattern
        # Include the explanation as context so users understand what's happening
        help_request = HelpRequest(question=action.question, context=agent_response.explanation)
        await ctx.request_info(help_request, str)

    @response_handler
    async def handle_help_response(
        self, original_request: HelpRequest, response: str, ctx: WorkflowContext[str]
    ) -> None:
        """Handle user's response to help request.

        Sends user response as observation to continue workflow.
        History is managed automatically by InMemoryHistoryProvider.
        """
        USER_LOGGER.info(f"Received user input: {response}")

        # Send as observation to continue workflow
        observation = f"User provided help response: {response}"
        await ctx.send_message(observation)


# ============================================================================
# Stage 1 — Planning: Help Handler
# ============================================================================


class PlanningHelpHandlerExecutor(Executor):
    """Help handler for the planning stage.

    Displays the current plan alongside the agent's question so the user
    can see the plan state while providing feedback.

    In autopilot mode, auto-approves the plan instead of requesting user input.
    """

    def __init__(self, llm_executor: BaseLLMExecutor, plan: "Plan", *, autopilot: bool = False):
        super().__init__(id="planning_help_handler")
        self.llm_executor = llm_executor
        self.plan = plan
        self.autopilot = autopilot

    @handler
    async def handle_help(self, agent_response: AgentResponse, ctx: WorkflowContext[str, Never]) -> None:
        action = agent_response.response
        if not isinstance(action, HelpResponse):
            return

        if self.autopilot:
            LOGGER.info("[Planning][Autopilot] Auto-approving plan")
            STATUS_LOGGER.info("PHASE: planning — autopilot auto-approving plan")
            await ctx.send_message("The plan looks good. Finalize it and proceed to execution.")
            return

        USER_LOGGER.info(f"[Planning] Agent asks: {action.question}")
        STATUS_LOGGER.info("PHASE: planning — requesting user feedback on plan")

        plan_view = self.plan.view()
        context = f"{agent_response.explanation}\n\n══ Current Plan ══\n{plan_view}"

        help_request = HelpRequest(question=action.question, context=context)
        await ctx.request_info(help_request, str)

    @response_handler
    async def handle_help_response(self, _: HelpRequest, response: str, ctx: WorkflowContext[str]) -> None:
        USER_LOGGER.info(f"[Planning] User feedback: {response}")
        await ctx.send_message(f"User provided feedback on the plan: {response}")


# ============================================================================
# Stage 1 → Stage 2 Bridge
# ============================================================================


class PlanToExecutionBridgeExecutor(Executor):
    """Transitions from the planning stage to the execution stage.

    Receives the SolutionResponse from the planning LLM (which signals
    the plan is finalized) and sends a kickoff message to the execution LLM.
    """

    def __init__(self, plan: "Plan"):
        super().__init__(id="plan_to_exec_bridge")
        self.plan = plan

    @handler
    async def handle_plan_finalized(self, agent_response: AgentResponse, ctx: WorkflowContext[str]) -> None:
        STATUS_LOGGER.info("PHASE: transitioning from planning to execution")
        USER_LOGGER.info("Plan finalized — beginning execution phase")

        plan_view = self.plan.view()
        kickoff = (
            "The plan has been finalized and approved by the user. "
            "Begin executing the plan now.\n\n"
            f"{plan_view}\n\n"
            "Start with the first pending step."
        )
        await ctx.send_message(kickoff)


# ============================================================================
# Stage 2 → Stage 3 Bridge
# ============================================================================


class ExecToPresentationBridgeExecutor(Executor):
    """Transitions from the execution stage to the presentation stage.

    Receives the SolutionResponse from the execution LLM (which signals all
    steps are done) and sends a kickoff message to the presentation LLM.
    """

    def __init__(self, plan: "Plan"):
        super().__init__(id="exec_to_present_bridge")
        self.plan = plan

    @handler
    async def handle_execution_done(self, agent_response: AgentResponse, ctx: WorkflowContext[str]) -> None:
        STATUS_LOGGER.info("PHASE: transitioning from execution to presentation")
        USER_LOGGER.info("Execution complete — beginning presentation phase")

        plan_view = self.plan.view()
        solution_summary = ""
        if isinstance(agent_response.response, SolutionResponse):
            solution_summary = agent_response.response.solution

        kickoff = (
            "Execution is complete. Present the results to the user for review.\n\n"
            f"Plan status:\n{plan_view}\n\n"
            f"Execution summary:\n{solution_summary}\n\n"
            "Compose a clear summary and present it to the user via a HelpResponse."
        )
        await ctx.send_message(kickoff)


# ============================================================================
# Stage 3 — Presentation: Help Handler
# ============================================================================


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


class PresentationHelpHandlerExecutor(Executor):
    """Help handler for the presentation stage.

    Displays the plan results alongside the agent's summary and asks the
    user to either **accept** the results (terminating the workflow) or
    provide **revision feedback** (routing back to the execution stage).

    In autopilot mode, auto-accepts the results and terminates the workflow.
    """

    def __init__(
        self,
        presentation_llm_executor: BaseLLMExecutor,
        execution_llm_executor: BaseLLMExecutor,
        plan: "Plan",
        *,
        autopilot: bool = False,
    ):
        super().__init__(id="presentation_help_handler")
        self.presentation_llm = presentation_llm_executor
        self.execution_llm = execution_llm_executor
        self.plan = plan
        self.autopilot = autopilot

    @handler
    async def handle_help(
        self, agent_response: AgentResponse, ctx: WorkflowContext[AgentResponse, SolutionResponse]
    ) -> None:
        action = agent_response.response
        if not isinstance(action, HelpResponse):
            return

        if self.autopilot:
            LOGGER.info("[Presentation][Autopilot] Auto-accepting results")
            STATUS_LOGGER.info("PHASE: presentation — autopilot auto-accepting results")
            plan_view = self.plan.view()
            solution = SolutionResponse(
                action="solution",
                solution=f"Results auto-accepted (autopilot mode).\n\n{plan_view}",
                provenance="Plan-then-execute workflow (autopilot)",
            )
            await ctx.yield_output(solution)
            return

        USER_LOGGER.info(f"[Presentation] Agent presents: {action.question}")
        STATUS_LOGGER.info("PHASE: presentation — awaiting user acceptance or revision")

        plan_view = self.plan.view()
        context = (
            f"{agent_response.explanation}\n\n"
            f"══ Plan Results ══\n{plan_view}\n\n"
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
            USER_LOGGER.info("[Presentation] User accepted results")
            STATUS_LOGGER.info("PHASE: results accepted — workflow complete")

            plan_view = self.plan.view()
            solution = SolutionResponse(
                action="solution",
                solution=f"Results accepted by user.\n\n{plan_view}",
                provenance="Plan-then-execute workflow",
            )
            await ctx.yield_output(solution)
        else:
            # ── User requested revisions → route back to execution ──
            USER_LOGGER.info(f"[Presentation] User requested revisions: {response}")
            STATUS_LOGGER.info("PHASE: revision requested — returning to execution")

            revision_prompt = (
                f"The user reviewed the results and requested revisions:\n\n"
                f'"{response}"\n\n'
                f"Review the current plan with view_plan, make the necessary changes, "
                f"and re-execute the affected steps. When done, produce a SolutionResponse "
                f"with updated results."
            )
            await ctx.send_message(revision_prompt)
