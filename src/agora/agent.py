"""
AgoraAgent - concrete agent implementation using Microsoft Agent Framework (MAF).

This module provides a MAF-based implementation with the following:
- agent_framework Workflows with explicit Executors
- Pydantic models for structured outputs
- State graph with nodes and edges where executors maintain their own state
- Catalog search (search_tools) for dynamic tool discovery
- Auto-discovered MCP server tools (execute_code, sessions) available from the start
"""

import asyncio
import logging
import os
from typing import Callable, Optional, Type, TYPE_CHECKING

from agent_framework import (
    Message,
    WorkflowBuilder,
    Case,
    Default,
)
from agent_framework.azure import AzureOpenAIChatClient

from auth import create_entra_token_provider
from data_lake.tools.data_lake import create_data_lake_search_tool, is_data_lake_configured
from domains.domain_registry import get_domain_registry
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
    ContinueHandlerExecutor,
    HelpHandlerExecutor,
)
from .response_models import SolutionResponse, ContinueResponse, HelpResponse
from .prompts.renderer import render_system_prompt

if TYPE_CHECKING:
    from agent_framework import Workflow
    from tools import ToolSearchBackend

# Basic logging setup
logging.basicConfig(level=logging.INFO)
LOGGER = logging.getLogger(__name__)
USER_LOGGER = logging.getLogger("user")


# ============================================================================
# AgoraAgent - Concrete Agent Implementation
# ============================================================================


class AgoraAgent:
    """
    Concrete Microsoft Agent Framework agent.

    All registered MCP servers are auto-discovered from server_registry.yaml.
    Each server's code execution and session management tools are available
    from the start — tool setup happens at the agent level and is passed
    to the executor.
    """

    def __init__(
        self,
        domain_prompt_path: Optional[str] = None,
        llm: str = "gpt-4o",
        max_iterations: int = 500,
        user_token: str = "",
        search_backend: Optional[Type["ToolSearchBackend"]] = None,
        context_providers: Optional[list] = None,
        middleware: Optional[list] = None,
        skill_paths: Optional[list[str]] = None,
        enable_toolmaker: bool = False,
        toolmaker_llm: Optional[str] = None,
    ):
        """
        Initialize the agent.

        Args:
            domain_prompt_path: Path to domain-specific Jinja template
            llm: Model name (e.g., "gpt-4o", "gpt-4o-mini")
            max_iterations: Maximum agent iterations (tracked by BaseLLMExecutor)
            user_token: Optional user's bearer token for OBO flow.
                - Local development: Leave as None. Uses Azure CLI credentials (az login).
                - Production: Pass user's token from Authorization header for OBO flow.
                  Requires ENTRA_CLIENT_SECRET configuration.
            search_backend: Tool search backend class.  Defaults to BM25ToolSearchBackend.
            context_providers: Optional list of MAF
                :class:`~agent_framework.BaseContextProvider` instances to
                register with the underlying agent (e.g. a
                :class:`~middleware.decision_log.DecisionLogContextProvider`).
            middleware: Optional list of MAF middleware instances
                (e.g. :class:`~middleware.decision_log.DecisionLogChatMiddleware`)
                to register with the underlying agent.
            skill_paths: Optional list of directories to scan for SKILL.md
                files that will be auto-advertised via ``SkillsProvider``.
                Defaults to ``[]`` (no auto-advertised skills).  Pass
                planning skill paths here when attaching planning tools.
            enable_toolmaker: If True, adds the create_tool_from_repo tool that allows
                the agent to dynamically create new domain tools from GitHub repositories
                using the ToolMaker agent. The agent will ask the user for a repo URL
                before invoking ToolMaker.
            toolmaker_llm: Model name for the ToolMaker agent. Defaults to the main
                agent's llm if not specified.
        """
        # Domain prompt paths — seeded with initial prompt, extended by MCP discovery
        self._loaded_domain_prompts: list[str] = []
        if domain_prompt_path:
            self._loaded_domain_prompts.append(domain_prompt_path)

        self._search_backend_cls = search_backend
        self._user_token = user_token

        # Toolmaker initialization
        self.enable_toolmaker = enable_toolmaker
        self.toolmaker_llm = toolmaker_llm or llm

        # Extra context providers passed through to the MAF Agent
        self._context_providers: list = context_providers or []

        # Extra middleware passed through to the MAF Agent
        self._middleware: list = middleware or []

        # Skill paths for SkillsProvider (auto-advertised skills).
        # Empty by default; pass planning skill paths when using planning tools.
        self._skill_paths: list[str] = skill_paths or []

        # Cached LLM executor — reused across workflow rebuilds so that
        # conversation history persists across multiple run() calls.
        self._llm_executor: BaseLLMExecutor | None = None

        # Render system prompt from Jinja template
        self.system_prompt = render_system_prompt(
            domain_prompt_path=domain_prompt_path,
            enable_toolmaker=enable_toolmaker,
        )

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
            function_invocation_configuration={
                "include_detailed_errors": True,
            },
        )

    def _build_tools(self) -> tuple[list, list[str]]:
        """Build the complete tool list for the agent.

        Sets up:
        - MCP server tools (execute_code + session management) for each registered server
        - Domain prompt resolution from discovered servers
        - search_tools FunctionTool for tool catalog search

        Returns:
            Tuple of (tools list, error messages list).
        """
        tools: list = []
        errors: list[str] = []
        domain_registry = get_domain_registry()

        # MCP tools for each registered server
        mcp_registry = get_mcp_registry()
        for server_name in mcp_registry.list_servers():
            try:
                mcp_tool = create_mcp_tools(server_name)
                if mcp_tool is not None:
                    tools.append(mcp_tool)
                    LOGGER.info("Added MCP tools for server '%s'", server_name)

                    # Resolve domain prompts for this server
                    prompt_path = domain_registry.get_domain_prompt_path(server_name)
                    if prompt_path and prompt_path not in self._loaded_domain_prompts:
                        self._loaded_domain_prompts.append(prompt_path)
                        LOGGER.info("Domain prompt '%s' from server '%s'", prompt_path, server_name)
                else:
                    msg = f"MCP tools for server '{server_name}' returned None"
                    LOGGER.warning(msg)
                    errors.append(msg)
            except Exception as e:
                msg = f"Failed to create MCP tools for server '{server_name}': {e}"
                LOGGER.error(msg)
                errors.append(msg)

        # Re-render system prompt if new domain prompts were discovered
        if self._loaded_domain_prompts:
            self.system_prompt = render_system_prompt(
                domain_prompt_paths=list(self._loaded_domain_prompts),
                enable_toolmaker=self.enable_toolmaker,
            )
            LOGGER.info("Rendered system prompt with domain prompts: %s", self._loaded_domain_prompts)

        # search_tools FunctionTool
        try:
            if self._search_backend_cls is None:
                search_backend = BM25ToolSearchBackend()
            else:
                search_backend = self._search_backend_cls(user_token=self._user_token)

            search_tools_fn = create_search_tools_function(search_backend)
            tools.append(search_tools_fn)
            LOGGER.info("Created search_tools FunctionTool for tool catalog search")
        except Exception as e:
            msg = f"Failed to create search_tools: {e}"
            LOGGER.error(msg)
            errors.append(msg)

        return tools, errors

    async def _build_workflow(self) -> "Workflow":
        """
        Build the agent workflow using MAF WorkflowBuilder.

        Workflow Graph:

        Start (prompt string) → LLM → AgentResponse → [Solution / Continue / Help]
                                 ↑                          |           |
                                 └──────────────────────────┘      HelpHandler
                                                                        |
                                                                        └────────────────────► LLM

        Tools are assembled at the agent level and passed to the executor.
        The agent discovers domain tools via ``search_tools`` and invokes
        them programmatically inside ``execute_code``.

        State is tracked internally by BaseLLMExecutor (conversation history, iteration count).
        Context compaction is handled automatically by the MAF-native CompactionProvider
        registered inside BaseLLMExecutor.
        Messages between nodes are just the data: prompt strings, AgentResponse objects, observation strings.
        """
        # Reuse the cached LLM executor if available (preserves conversation history)
        if self._llm_executor is not None:
            llm_executor = self._llm_executor
        else:
            # Build all agent tools (MCP + search_tools + data lake)
            agent_tools, tool_errors = self._build_tools()

            # DataLake search tool (async, created separately)
            if is_data_lake_configured():
                try:
                    data_lake_tool = await create_data_lake_search_tool(user_token=self.user_token)
                    agent_tools.append(data_lake_tool)
                    LOGGER.info("Created search_data_lake_catalog tool for DataLake artifact discovery")
                except Exception as e:
                    msg = f"Failed to create data lake search tool: {e}"
                    LOGGER.error(msg)
                    tool_errors.append(msg)

            if tool_errors:
                LOGGER.warning("Some tools failed to build: %s", "; ".join(tool_errors))

            # query_state_graph FunctionTool — lazily discovers tools from
            # MCP servers on first query so domain meta-tools are available
            state_graph_fn = create_query_state_graph_function()
            agent_tools.append(state_graph_fn)
            LOGGER.info("Created query_state_graph FunctionTool for workflow exploration")

            # load_skill FunctionTool — reads full SKILL.md content by name,
            # replacing the SkillsProvider context-injection for domain skills
            load_skill_fn = create_load_skill_function()
            agent_tools.append(load_skill_fn)
            LOGGER.info("Created load_skill FunctionTool for on-demand skill loading")

            # Create executors — all tools passed in, executor just runs the LLM
            llm_executor = BaseLLMExecutor(
                self.chat_client,
                self.system_prompt,
                self.max_iterations,
                tools=agent_tools,
                skill_paths=self._skill_paths,
                context_providers=self._context_providers,
                middleware=self._middleware,
            )
            self._llm_executor = llm_executor
            self._tool_build_errors = tool_errors

            # Add toolmaker tool if enabled — created after the executor so it can
            # share the executor's mutable tool-loading state (retrieved_tools, flags).
            if self.enable_toolmaker:
                try:
                    from tools.toolmaker import create_toolmaker_function

                    toolmaker_tool = create_toolmaker_function(
                        llm=self.toolmaker_llm,
                        max_iterations=self.max_iterations,
                        base_tools=llm_executor.base_tools,
                    )
                    llm_executor.base_tools.append(toolmaker_tool)
                except Exception as e:
                    msg = f"Failed to create toolmaker tool: {e}"
                    LOGGER.error(msg)
                    self._tool_build_errors.append(msg)

        solution_handler = SolutionHandlerExecutor()
        continue_handler = ContinueHandlerExecutor()
        help_handler = HelpHandlerExecutor(llm_executor=llm_executor)

        # Build workflow
        builder = WorkflowBuilder(
            start_executor=llm_executor,
        )

        # LLM → Handlers (switch-case based on AgentResponse.response type)
        builder.add_switch_case_edge_group(
            llm_executor,
            [
                Case(
                    condition=lambda msg: isinstance(msg.response, SolutionResponse),
                    target=solution_handler,
                ),
                Case(
                    condition=lambda msg: isinstance(msg.response, ContinueResponse),
                    target=continue_handler,
                ),
                # Help is the default case - if response type is unexpected or malformed,
                # asking for clarification is safer than forcing a solution
                Default(target=help_handler),
            ],
        )

        # Continue → LLM (autonomous loop, context compaction handled by CompactionProvider)
        builder.add_edge(continue_handler, llm_executor)

        # Help → LLM (loop with context compaction handled by CompactionProvider)
        builder.add_edge(help_handler, llm_executor)

        # Solution is terminal node (yields_output)

        return builder.build()

    async def _run_workflow(self, prompt: str, input_handler: Callable) -> Message:
        """
        Run a single workflow pass: build workflow, execute until solution or exhaustion.

        Handles the help-request loop internally: when the agent requests user
        clarification, it collects input via *input_handler* and resumes.

        Returns the final Message for this pass.
        """
        try:
            workflow = await self._build_workflow()
        except Exception as e:
            LOGGER.error("Failed to build workflow: %s", e)
            error_detail = f"Workflow failed to build: {e}"
            user_response = await input_handler(
                f"The agent encountered an error during setup: {error_detail}. How would you like to proceed?",
                "Tool or workflow initialization failed.",
            )
            return Message(role="assistant", text=user_response)

        # If some tools failed to build, prepend context so the agent is aware
        tool_errors = getattr(self, "_tool_build_errors", [])
        if tool_errors:
            error_context = (
                "[NOTE: The following tools failed to initialize: "
                + "; ".join(tool_errors)
                + ". You may have reduced capabilities. "
                "Ask the user for help if you need these tools.]"
            )
            prompt = f"{error_context}\n\n{prompt}"

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

    async def run(self, prompt: str, input_handler: Callable | None = None) -> str:
        """
        Send a message and get a response.  Conversation state is preserved
        between calls, so this works for both single-turn and multi-turn usage::

            async with AgoraAgent(...) as agent:
                print(await agent.run("My name is Alice and I love hiking."))
                print(await agent.run("What do you remember about me?"))  # remembers Alice

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
