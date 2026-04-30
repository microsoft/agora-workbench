"""
Workflow Executors for AgoraAgent.

This module contains the executor implementations for the MAF workflow:
- BaseLLMExecutor: Generic LLM executor — calls the LLM with pre-built tools and returns structured AgentResponse
- SolutionHandlerExecutor: Handles final solution (terminal node)
- HelpHandlerExecutor: Handles help requests with user input
"""

import httpx
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Never

from agent_framework import (
    Agent,
    MCPStreamableHTTPTool,
    CharacterEstimatorTokenizer,
    CompactionProvider,
    InMemoryHistoryProvider,
    Message,
    Executor,
    SkillsProvider,
    SlidingWindowStrategy,
    SummarizationStrategy,
    TokenBudgetComposedStrategy,
    WorkflowContext,
    handler,
    response_handler,
)

from context_managers.compaction import SkillAwareToolCompactionStrategy

from .response_models import (
    AgentResponse,
    ContinueResponse,
    HelpResponse,
    SolutionResponse,
)

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
# Workflow Executors
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
                    pass
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
                        pass
            else:
                surviving.append(tool)
        self.base_tools[:] = surviving

    # ------------------------------------------------------------------
    # Override: execute with tool-loading rerun loop
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
- Use "continue" if you made progress but the task is NOT complete (e.g., you encountered an error and need to retry, or you have partial results and need more steps).
- ONLY use "help" if you GENUINELY need user clarification (missing parameters, ambiguous requirements). Do NOT use "help" to announce what you're about to do - just do it via tool calls.

- Use one of these EXACT schemas:

For a SOLUTION (final answer - ONLY when task is DONE with actual results):
{{
  "explanation": "your reasoning",
  "response": {{"action": "solution", "solution": "your final answer with actual results here", "provenance": "optional: what tools/data used"}},
  "status": "optional status message"
}}

For CONTINUING work (task NOT complete - need more tool calls or iterations):
{{
  "explanation": "your reasoning",
  "response": {{"action": "continue", "reasoning": "what was done so far, what went wrong, and what to do next"}},
  "status": "optional status message"
}}

For asking for HELP/clarification (ONLY when genuinely blocked by missing info):
{{
  "explanation": "your reasoning",
  "response": {{"action": "help", "question": "specific question about missing parameter or ambiguous requirement"}},
  "status": "optional status message"
}}

CRITICAL: 
- Use the EXACT field names shown above (solution, reasoning, question - NOT result, answer, etc.)
- "action" must be one of: "solution", "continue", "help" (NOT "execute" or "retrieval")
- Do NOT return a "solution" that describes future intentions - use "continue" instead!
- If there were errors or incomplete results, use "continue" so you can retry."""

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

        # All retries failed - fall back to ContinueResponse to let the agent retry
        LOGGER.error(f"All {max_retries} retries failed. Falling back to ContinueResponse.")

        # Extract reasoning from the raw text to give the LLM context on next iteration
        fallback_reasoning = response_text[:500] if response_text else "Previous response could not be parsed."

        return AgentResponse(
            explanation="The LLM returned an unstructured response that could not be parsed as JSON after multiple retries.",
            response=ContinueResponse(
                action="continue",
                reasoning=fallback_reasoning,
            ),
            status="Failed to parse LLM response - continuing to retry",
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


class ContinueHandlerExecutor(Executor):
    """Executor that handles continue responses — agent needs more iterations.

    Receives: AgentResponse (with ContinueResponse action)
    Action: Extracts reasoning and sends as observation for the next LLM iteration.
    Sends: observation string (forwarded to ContextHandler → LLM)

    This enables autonomous looping without requiring user input.
    """

    def __init__(self):
        super().__init__(id="continue_handler")

    @handler
    async def handle_continue(self, agent_response: AgentResponse, ctx: WorkflowContext[str]) -> None:
        """Handle continue response — forward reasoning as observation to continue the loop."""
        action = agent_response.response
        if not isinstance(action, ContinueResponse):
            return

        USER_LOGGER.info(f"Agent continuing: {action.reasoning[:200]}")
        STATUS_LOGGER.info("PHASE: continuing iteration")

        observation = f"Continue working. Your previous progress: {action.reasoning}"
        await ctx.send_message(observation)


class HelpHandlerExecutor(Executor):
    """Executor that handles help requests by prompting user for input.

    Receives: AgentResponse (with HelpResponse action)
    Action: Requests user input via ctx.request_info, waits for response
    Sends: observation string (user's response to help question)

    Uses MAF's request_info pattern: emits HelpRequest, waits for string response.
    """

    def __init__(self, llm_executor: "BaseLLMExecutor"):
        super().__init__(id="help_handler")
        self.llm_executor = llm_executor

    @handler
    async def handle_help(self, agent_response: AgentResponse, ctx: WorkflowContext) -> None:
        """Handle help request - emit request for user input.

        Uses ctx.request_info to pause workflow and request user input.
        The response will be handled by handle_help_response.
        """
        action = agent_response.response
        if not isinstance(action, HelpResponse):
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
