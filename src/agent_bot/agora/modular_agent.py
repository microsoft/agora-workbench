"""
ModularAgent — hook-driven, standalone agent for integration-heavy scenarios.

This module provides a fully-featured MAF agent whose behaviour can be
composed at construction time through plain callables (hooks).  Unlike
:class:`~agent_bot.agora.agent.AgoraAgent`, ``ModularAgent`` is **not** a
subclass of ``AgoraAgent``; it owns all of its behaviour directly so
integrators can tailor every aspect without the constraints of an inheritance
hierarchy.

See :class:`ModularAgent` for the full parameter reference, or
``agent_bot/agora/README.md`` for narrative documentation and examples.
"""

import asyncio
import logging
import os
from typing import Any, Callable, Optional, Type, TYPE_CHECKING

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

LOGGER = logging.getLogger(__name__)


# ============================================================================
# ModularAgent
# ============================================================================


class ModularAgent:
    """Hook-driven agent for scenarios that need fine-grained integration control.

    ``ModularAgent`` is a **standalone** MAF agent — it shares the same
    public interface as :class:`~agent_bot.agora.agent.AgoraAgent` but does
    **not** inherit from it.  Every aspect of its behaviour can be overridden
    at construction time through plain callables, without subclassing or
    monkey-patching internal methods.

    Default behaviour (no hooks supplied) is identical to ``AgoraAgent``:

    * MCP server tools are auto-discovered from ``server_registry.yaml``.
    * A BM25 ``search_tools`` function is built from the local tool catalog.
    * The MAF workflow runs interactively, pausing on ``HelpResponse`` to
      collect user input.

    Provide hook callables to selectively replace or augment any of these
    steps.  See the parameter reference below and
    ``agent_bot/agora/README.md`` for worked examples.

    Args:
        domain_prompt_path (str | None):
            Path to a Jinja template that provides the domain-specific section
            of the system prompt.  The template is rendered once at construction
            time and again after MCP discovery (if new domain prompts are
            found).  Pass ``None`` to use only the built-in base prompt.

        llm (str):
            Azure OpenAI deployment name to use as the primary LLM, e.g.
            ``"gpt-4o"`` or ``"gpt-4o-mini"``.  Passed to
            ``AzureOpenAIChatClient``.  Default: ``"gpt-4o"``.

        max_iterations (int):
            Maximum number of LLM inference calls the agent is allowed to make
            per workflow run before it terminates.  Tracked by
            ``BaseLLMExecutor``.  Default: ``500``.

        user_token (str):
            Bearer token forwarded to MCP servers for OBO (on-behalf-of) flows.
            Leave as an empty string for local development — the agent will
            fall back to ``AzureCliCredential`` (``az login``).  In production,
            pass the user's token from the ``Authorization`` header.  Requires
            ``ENTRA_CLIENT_SECRET`` to be configured server-side.
            Default: ``""``.

        search_backend (type[ToolSearchBackend] | None):
            *Class* (not instance) of the ``ToolSearchBackend`` to use for
            ``search_tools``.  The class is instantiated with
            ``search_backend(user_token=user_token)``.  Pass ``None`` to use
            the default ``BM25ToolSearchBackend`` (local, zero-dependency).
            Ignored when ``search_tool_factory`` is provided.
            Default: ``None``.

        context_providers (list | None):
            Initial list of MAF ``BaseContextProvider`` instances to register
            with the underlying ``BaseLLMExecutor``.  Providers injected here
            are applied *before* ``context_provider_modulator`` runs.
            Example: ``[DecisionLogContextProvider()]``.
            Default: ``None`` (empty list).

        middleware (list | None):
            Initial list of MAF middleware instances to register with the
            underlying ``BaseLLMExecutor``.  Applied *before*
            ``middleware_modulator`` runs.
            Example: ``[DecisionLogChatMiddleware()]``.
            Default: ``None`` (empty list).

        autopilot (bool):
            When ``True``, the agent never pauses for user input.  Any
            ``HelpResponse`` or ``request_info`` pause from the workflow is
            automatically resolved with a synthetic "best effort" message so
            the workflow continues unattended.  Useful for batch runs,
            CI pipelines, and automated evaluations.  Default: ``False``.

        tool_modulator (Callable[[list], list] | None):
            Called with the *complete* assembled tool list just before it is
            passed to ``BaseLLMExecutor``.  The callable receives the list as
            built so far (MCP tools + search tool + skill tool + sub-agent
            tools) and must return the final list.  Use this to add, remove, or
            reorder tools without subclassing.  Return ``None`` to leave the
            list unchanged.  Default: ``None``.

            Signature: ``(tools: list) -> list | None``

        search_tool_factory (Callable[[type | None, str], Any] | None):
            Factory that produces the ``search_tools`` function tool.  Called
            with ``(search_backend_cls, user_token)`` where
            ``search_backend_cls`` is the value passed to ``search_backend``
            (may be ``None``).  When not provided, the default BM25 or
            configured backend is used.  Default: ``None``.

            Signature: ``(search_backend_cls: type | None, user_token: str) -> Any``

        skill_advertiser (Callable[[list[str]], str] | None):
            Called with the list of discovered skill names (populated by
            ``auto_skill_discovery``) and must return a string to append to the
            system prompt.  The advertisement is appended once, at the end of
            ``_build_tools()``.  Has no effect when
            ``enable_auto_skill_discovery=False`` or no skills are discovered.
            Default: ``None``.

            Signature: ``(skill_names: list[str]) -> str``

        skill_search_tool_factory (Callable[[list[str]], Any | None] | None):
            Factory that produces an optional skill-search tool.  Called with
            the list of discovered skill names.  Return ``None`` to skip adding
            the tool.  Default: ``None``.

            Signature: ``(skill_names: list[str]) -> Any | None``

        context_provider_modulator (Callable[[list], list] | None):
            Called at construction time with a copy of ``context_providers``
            (the value supplied to the constructor).  The returned list
            replaces ``self._context_providers``.  Return ``None`` to leave it
            unchanged.  Useful for conditionally appending or filtering
            providers without subclassing.  Default: ``None``.

            Signature: ``(providers: list) -> list | None``

        middleware_modulator (Callable[[list], list] | None):
            Called at construction time with a copy of ``middleware``
            (the value supplied to the constructor).  The returned list
            replaces ``self._middleware``.  Return ``None`` to leave it
            unchanged.  Default: ``None``.

            Signature: ``(middleware: list) -> list | None``

        auto_tool_discovery (Callable[[], list] | None):
            Optional callable invoked during ``_build_tools()`` to produce
            additional tools that are appended **after** MCP tools but
            **before** the search tool.  Return ``None`` or an empty list to
            produce no extra tools.  Default: ``None``.

            Signature: ``() -> list | None``

        enable_auto_tool_discovery (bool):
            When ``True`` (the default), MCP server tools are auto-discovered
            from ``server_registry.yaml`` during ``_build_tools()``.  Set to
            ``False`` to skip MCP discovery entirely — useful when you want to
            supply all tools manually via ``auto_tool_discovery`` and
            ``tool_modulator``.  Default: ``True``.

        auto_skill_discovery (Callable[[], list[str]] | None):
            Called at construction time (when
            ``enable_auto_skill_discovery=True``) to populate
            ``self._discovered_skills``.  The returned list of skill names is
            passed to ``skill_advertiser`` and ``skill_search_tool_factory``.
            Return ``None`` to produce no skills.  Default: ``None``.

            Signature: ``() -> list[str] | None``

        enable_auto_skill_discovery (bool):
            When ``True``, ``auto_skill_discovery`` is called at construction
            time to populate ``self._discovered_skills``.  If ``False`` (the
            default), ``auto_skill_discovery`` is never invoked.
            Default: ``False``.

        required_tools (list[str] | None):
            List of tool names that *must* be present in the assembled tool
            list.  After all tools are assembled (and after
            ``tool_modulator`` runs), ``_build_tools()`` checks that every
            name in this list appears among the tools.  Missing tools are
            reported as errors in the returned errors list.  Default: ``None``.

        sub_agent_tool_factories (list[Callable[[], Any | None]] | None):
            List of zero-argument callables, each of which returns a tool to
            append to the agent's tool list (or ``None`` to skip).  Called
            during ``_build_tools()``, after the skill search tool and before
            ``tool_modulator``.  Use this to attach tools that wrap other
            agents (sub-agents) at construction time.  Default: ``None``.

            Each factory signature: ``() -> Any | None``
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
        *,
        autopilot: bool = False,
        tool_modulator: Optional[Callable[[list], list | None]] = None,
        search_tool_factory: Optional[Callable[[Any, str], Any]] = None,
        skill_advertiser: Optional[Callable[[list[str]], str]] = None,
        skill_search_tool_factory: Optional[Callable[[list[str]], Optional[Any]]] = None,
        context_provider_modulator: Optional[Callable[[list], list | None]] = None,
        middleware_modulator: Optional[Callable[[list], list | None]] = None,
        auto_tool_discovery: Optional[Callable[[], list]] = None,
        enable_auto_tool_discovery: bool = True,
        auto_skill_discovery: Optional[Callable[[], list[str]]] = None,
        enable_auto_skill_discovery: bool = False,
        required_tools: Optional[list[str]] = None,
        sub_agent_tool_factories: Optional[list[Callable[[], Optional[Any]]]] = None,
    ):
        # ── base agent state (mirrors AgoraAgent.__init__) ────────────────────
        self._loaded_domain_prompts: list[str] = []
        if domain_prompt_path:
            self._loaded_domain_prompts.append(domain_prompt_path)

        self._search_backend_cls = search_backend
        self._user_token = user_token

        self._context_providers: list = context_providers or []
        self._middleware: list = middleware or []

        # Cached LLM executor — reused across workflow rebuilds so that
        # conversation history persists across multiple run() calls.
        self._llm_executor: BaseLLMExecutor | None = None

        self.system_prompt = render_system_prompt(
            domain_prompt_path=domain_prompt_path,
        )

        self.max_iterations = max_iterations
        self.llm_model = llm
        self.user_token = user_token

        self.chat_client = self._create_chat_client(llm)

        # ── hook storage ──────────────────────────────────────────────────────
        self.autopilot = autopilot
        self._tool_modulator = tool_modulator
        self._search_tool_factory = search_tool_factory
        self._skill_advertiser = skill_advertiser
        self._skill_search_tool_factory = skill_search_tool_factory
        self._auto_tool_discovery = auto_tool_discovery
        self._enable_auto_tool_discovery = enable_auto_tool_discovery
        self._required_tools = set(required_tools or [])
        self._sub_agent_tool_factories = sub_agent_tool_factories or []
        self._discovered_skills: list[str] = []

        # Apply constructor-time modulators for context providers and middleware.
        if context_provider_modulator:
            modulated = context_provider_modulator(list(self._context_providers))
            if modulated is not None:
                self._context_providers = modulated
        if middleware_modulator:
            modulated_mw = middleware_modulator(list(self._middleware))
            if modulated_mw is not None:
                self._middleware = modulated_mw

        # Run skill discovery eagerly if enabled.
        if enable_auto_skill_discovery and auto_skill_discovery:
            try:
                discovered = auto_skill_discovery()
                if discovered is not None:
                    self._discovered_skills = discovered
            except Exception as e:
                LOGGER.warning("Failed to auto-discover skills: %s", e)

    # ── async context manager ─────────────────────────────────────────────────

    async def __aenter__(self):
        """Enter async context manager."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Exit async context manager — clean up MCP connections."""
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

    # ── internal helpers ──────────────────────────────────────────────────────

    def _create_chat_client(
        self,
        deployment_name: str,
        azure_endpoint: Optional[str] = None,
        aoai_scope: Optional[str] = None,
        api_version: Optional[str] = None,
    ) -> AzureOpenAIChatClient:
        """Create an ``AzureOpenAIChatClient`` with Entra ID authentication."""
        endpoint = azure_endpoint or os.getenv("AZURE_OPENAI_ENDPOINT")
        if not endpoint:
            raise EnvironmentError("Environment variable AZURE_OPENAI_ENDPOINT not found.")

        scope = aoai_scope or os.getenv("AOAI_SCOPE")
        if not scope:
            raise EnvironmentError("Environment variable AOAI_SCOPE not found.")

        api_version = api_version or os.getenv("API_VERSION")
        if not api_version:
            raise EnvironmentError("Environment variable API_VERSION not found.")

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

    @staticmethod
    def _tool_name(tool: Any) -> str:
        """Return a best-effort display name for a tool object."""
        return getattr(tool, "name", "") or getattr(tool, "__name__", "") or str(tool)

    def _maybe_advertise_skills(self) -> None:
        """Append the skill advertisement to the system prompt if configured."""
        if not self._skill_advertiser or not self._discovered_skills:
            return

        try:
            skill_ad = self._skill_advertiser(list(self._discovered_skills))
        except Exception as e:
            LOGGER.warning("Failed to generate skill advertisement: %s", e)
            return

        if skill_ad and skill_ad not in self.system_prompt:
            self.system_prompt = f"{self.system_prompt}\n\n{skill_ad}"

    # ── tool assembly ─────────────────────────────────────────────────────────

    def _build_tools(self) -> tuple[list, list[str]]:
        """Assemble the full tool list for the agent.

        Assembly order:
        1. MCP server tools (when ``enable_auto_tool_discovery=True``)
        2. Tools from ``auto_tool_discovery`` callable
        3. Search tool (from ``search_tool_factory`` or default BM25)
        4. Skill search tool (from ``skill_search_tool_factory``)
        5. Sub-agent tools (from ``sub_agent_tool_factories``)
        6. ``tool_modulator`` post-processing pass
        7. ``required_tools`` validation
        8. ``skill_advertiser`` system-prompt append

        Returns:
            Tuple of ``(tools, errors)`` where *errors* is a list of
            human-readable strings describing any non-fatal failures.
        """
        tools: list = []
        errors: list[str] = []
        domain_registry = get_domain_registry()

        # 1. MCP server tools from registry.
        if self._enable_auto_tool_discovery:
            mcp_registry = get_mcp_registry()
            for server_name in mcp_registry.list_servers():
                try:
                    mcp_tool = create_mcp_tools(server_name)
                    if mcp_tool is not None:
                        tools.append(mcp_tool)
                        LOGGER.info("Added MCP tools for server '%s'", server_name)

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

        # 2. Custom auto-discovered tools.
        if self._auto_tool_discovery:
            try:
                discovered_tools = self._auto_tool_discovery()
                if discovered_tools is not None:
                    tools.extend(discovered_tools)
            except Exception as e:
                msg = f"Failed to auto-discover tools: {e}"
                LOGGER.error(msg)
                errors.append(msg)

        # Re-render system prompt if new domain prompts were discovered.
        if self._loaded_domain_prompts:
            self.system_prompt = render_system_prompt(
                domain_prompt_paths=list(self._loaded_domain_prompts),
            )

        # 3. Search tool.
        try:
            if self._search_tool_factory is not None:
                search_tool = self._search_tool_factory(self._search_backend_cls, self._user_token)
            else:
                if self._search_backend_cls is None:
                    search_backend = BM25ToolSearchBackend()
                else:
                    search_backend = self._search_backend_cls(user_token=self._user_token)
                search_tool = create_search_tools_function(search_backend)

            tools.append(search_tool)
            LOGGER.info("Created search_tools FunctionTool for tool catalog search")
        except Exception as e:
            msg = f"Failed to create search_tools: {e}"
            LOGGER.error(msg)
            errors.append(msg)

        # 4. Skill search tool.
        if self._skill_search_tool_factory:
            try:
                skill_tool = self._skill_search_tool_factory(list(self._discovered_skills))
                if skill_tool is not None:
                    tools.append(skill_tool)
            except Exception as e:
                msg = f"Failed to create skill search tool: {e}"
                LOGGER.error(msg)
                errors.append(msg)

        # 5. Sub-agent tools.
        for factory in self._sub_agent_tool_factories:
            try:
                sub_tool = factory()
                if sub_tool is not None:
                    tools.append(sub_tool)
            except Exception as e:
                msg = f"Failed to attach sub-agent tool: {e}"
                LOGGER.error(msg)
                errors.append(msg)

        # 6. Post-processing modulator.
        if self._tool_modulator:
            try:
                modulated = self._tool_modulator(list(tools))
                if modulated is not None:
                    tools = modulated
            except Exception as e:
                msg = f"Failed to modulate tools: {e}"
                LOGGER.error(msg)
                errors.append(msg)

        # 7. Required-tools validation.
        if self._required_tools:
            available_names = {self._tool_name(t) for t in tools}
            missing = sorted(self._required_tools - available_names)
            if missing:
                errors.append(f"Missing required tools: {', '.join(missing)}")

        # 8. Skill advertisement.
        self._maybe_advertise_skills()

        return tools, errors

    # ── workflow ──────────────────────────────────────────────────────────────

    async def _build_workflow(self) -> "Workflow":
        """Build (or reuse) the MAF workflow for this agent.

        Workflow graph::

            Start → LLM → [Solution | Continue | Help]
                     ↑         |           |
                     └─────────┘       HelpHandler
                                           |
                                           └──► LLM

        The LLM executor is cached so conversation history is preserved across
        successive :meth:`run`/:meth:`go` calls.
        """
        if self._llm_executor is not None:
            llm_executor = self._llm_executor
        else:
            agent_tools, tool_errors = self._build_tools()

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

            llm_executor = BaseLLMExecutor(
                self.chat_client,
                self.system_prompt,
                self.max_iterations,
                tools=agent_tools,
                context_providers=self._context_providers,
                middleware=self._middleware,
            )
            self._llm_executor = llm_executor
            self._tool_build_errors = tool_errors

        solution_handler = SolutionHandlerExecutor()
        continue_handler = ContinueHandlerExecutor()
        help_handler = HelpHandlerExecutor(llm_executor=llm_executor)

        builder = WorkflowBuilder(start_executor=llm_executor)
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
                Default(target=help_handler),
            ],
        )
        builder.add_edge(continue_handler, llm_executor)
        builder.add_edge(help_handler, llm_executor)

        return builder.build()

    async def _run_workflow(self, prompt: str, input_handler: Callable) -> Message:
        """Execute a single workflow pass.

        In interactive mode (``autopilot=False``), any ``request_info`` events
        produced by the workflow are forwarded to *input_handler* and the
        workflow is resumed with the collected responses.

        In autopilot mode (``autopilot=True``), ``request_info`` pauses are
        automatically resolved with a synthetic "best effort" message so the
        workflow continues without user interaction.
        """
        try:
            workflow = await self._build_workflow()
        except Exception as e:
            LOGGER.error("Failed to build workflow: %s", e)
            if self.autopilot:
                return Message(role="assistant", text=f"Workflow failed to build: {e}")
            user_response = await input_handler(
                f"The agent encountered an error during setup: {e}. How would you like to proceed?",
                "Tool or workflow initialization failed.",
            )
            return Message(role="assistant", text=user_response)

        tool_errors = getattr(self, "_tool_build_errors", [])
        if tool_errors:
            hint = " Ask the user for help if you need these tools." if not self.autopilot else ""
            error_context = (
                "[NOTE: The following tools failed to initialize: "
                + "; ".join(tool_errors)
                + f". You may have reduced capabilities.{hint}]"
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

            if self.autopilot:
                responses = {
                    req_event.request_id: (
                        "Autopilot mode: proceed with best effort using available context and assumptions."
                    )
                    for req_event in pending
                }
            else:
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
            if isinstance(output, HelpResponse):
                return Message(role="assistant", text=output.question)
            return Message(role="assistant", text=str(output))

        return Message(role="assistant", text="Workflow completed without final output")

    # ── public API ────────────────────────────────────────────────────────────

    async def run(self, prompt: str, input_handler: Callable | None = None) -> str:
        """Send a message and return the agent's response text.

        Conversation state is preserved between calls::

            async with ModularAgent(...) as agent:
                print(await agent.run("My name is Alice."))
                print(await agent.run("What do you remember about me?"))

        Args:
            prompt: The user's message.
            input_handler: Optional async callable ``(question, context) -> str``
                invoked when the agent issues a ``HelpResponse`` or
                ``request_info`` pause.  Ignored when ``autopilot=True``.
                Defaults to console input.

        Returns:
            The agent's final response as a plain string.
        """
        if input_handler is None:
            input_handler = self._default_input_handler
        result = await self._run_workflow(prompt, input_handler)
        return result.text

    async def go(self, prompt: str, input_handler: Callable | None = None) -> Message:
        """Execute the agent and return the raw :class:`~agent_framework.Message`.

        Prefer :meth:`run` for new code.  This method is kept for consistency
        with other agent classes in this repository.

        Args:
            prompt: The user's message.
            input_handler: Same as :meth:`run`.

        Returns:
            A :class:`~agent_framework.Message` with ``role="assistant"``.
        """
        if input_handler is None:
            input_handler = self._default_input_handler
        return await self._run_workflow(prompt, input_handler)

    @staticmethod
    async def _default_input_handler(question: str, context: str = "") -> str:
        """Console-based fallback input handler used when none is supplied."""
        if context:
            print(f"\n📋 Context: {context}")
        print(f"\n🤖 Agent needs help: {question}")
        return await asyncio.to_thread(input, "Your response: ")
