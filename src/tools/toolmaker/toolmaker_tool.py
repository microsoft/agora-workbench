"""
ToolMaker FunctionTool for AgoraAgent.

This module provides a `create_tool_from_repo` FunctionTool that invokes the ToolMaker agent
to autonomously create an MCP domain server from a GitHub repository. This allows AgoraAgent
to dynamically create new tools when a needed capability doesn't exist.

Usage:
    When AgoraAgent determines that no existing tool can satisfy a request, it can call
    `create_tool_from_repo` with a GitHub repo URL and description. ToolMaker will:
    1. Explore the repo to understand its capabilities
    2. Generate an MCP domain server that wraps the repo's functionality
    3. Build and test the domain in Docker
    4. Ask the user whether to keep the tool as reusable or session-only
    5. If reusable, register the domain so it persists across sessions

Example:
    AgoraAgent detects user wants to work with Roman numerals, but no such tool exists.
    → Calls create_tool_from_repo(
        repo_url="https://github.com/zopefoundation/roman",
        tool_description="Convert integers to Roman numerals"
      )
    → ToolMaker creates the 'roman' domain with a 'to_roman' tool
    → AgoraAgent can now use the new tool
"""

import logging
import os
from typing import Callable, Literal, Optional

import httpx
from pydantic import BaseModel, Field

from agent_framework import FunctionTool, MCPStreamableHTTPTool
from auth import create_entra_token_provider, BearerTokenAuth

USER_LOGGER = logging.getLogger("user")
LOGGER = logging.getLogger(__name__)


class CreateToolInput(BaseModel):
    """Input schema for create_tool_from_repo."""

    repo_url: str = Field(
        description="GitHub repository URL that the user has confirmed (e.g. 'https://github.com/zopefoundation/roman'). You MUST have asked the user to approve this URL before calling this tool."
    )
    tool_description: str = Field(
        description=(
            "Description of the tool capability needed. Be specific about the function(s) "
            "you want exposed, their inputs and outputs. "
            "Example: 'Convert integers to Roman numerals - function to_roman(n: int) -> str'"
        )
    )
    tool_name: Optional[str] = Field(
        default=None,
        description="Optional: Specific name for the tool function (e.g. 'to_roman'). If not provided, ToolMaker will infer a suitable name.",
    )
    domain_name: Optional[str] = Field(
        default=None,
        description="Optional: Name for the MCP domain (e.g. 'roman'). If not provided, derived from repo name.",
    )


class CreateToolResult(BaseModel):
    """Result from create_tool_from_repo."""

    success: bool = Field(description="Whether the tool was successfully created")
    domain_name: str = Field(default="", description="Name of the created domain")
    tool_name: str = Field(default="", description="Name of the created tool function")
    tool_description: str = Field(default="", description="Description of what the tool does")
    server_url: str = Field(default="", description="URL of the running MCP server")
    server_port: int = Field(default=8010, description="Port the MCP server is running on")
    error: str = Field(default="", description="Error message if creation failed")
    message: str = Field(default="", description="Summary message about the result")
    test_summary: str = Field(default="", description="Summary of test results")

    def to_string(self) -> str:
        """Format the result for LLM consumption."""
        if self.success:
            parts = [
                "✅ Successfully created and tested new tool!",
                f"  Domain: {self.domain_name}",
                f"  Tool: {self.tool_name}",
                f"  Description: {self.tool_description}",
                f"  Server URL: {self.server_url}",
                f"  Port: {self.server_port}",
            ]
            if self.test_summary:
                parts.append(f"\n  Test results:\n{self.test_summary}")
            parts.append(
                "\nThe tool has been loaded and is now available for use. "
                "You MUST call this tool now to answer the user's original question. "
                "Do NOT answer from your own knowledge — invoke the tool. "
                "Clearly state that the result came from the newly created tool. "
                "Do NOT call search_tools or create_tool_from_repo again — the tool is already loaded."
            )
            return "\n".join(parts)
        else:
            return (
                f"❌ Failed to create tool: {self.error}\n\n"
                "IMPORTANT: Tool creation failed. You MUST use a HelpResponse to ask the user for help. "
                "Do NOT use a SolutionResponse — the task is NOT complete. "
                "Do NOT silently fall back to answering from your own knowledge. "
                "Tell the user what went wrong and ask if they want to: "
                "(1) try again with different parameters, "
                "(2) provide a different repository URL, or "
                "(3) take a different approach entirely."
            )


async def _non_interactive_handler(question: str, context: str = "") -> str:
    """Auto-resolve user prompts without blocking.

    Used as the default input_handler when no explicit handler is provided,
    so that ToolMaker running as a sub-agent never blocks on console input.
    """
    LOGGER.info("Non-interactive auto-response for prompt: %s", question)
    q = question.lower()
    if "reusable" in q or "session" in q or "persist" in q:
        return "session"
    return "yes"


def create_toolmaker_function(
    llm: str = "gpt-5.1_2025-11-13",
    max_iterations: int = 500,
    auto_cleanup: bool = True,
    approval_mode: Literal["never_require", "always_require"] = "never_require",
    base_tools: Optional[list] = None,
    input_handler: Optional[Callable] = None,
) -> FunctionTool:
    """
    Create a `create_tool_from_repo` FunctionTool that invokes ToolMaker.

    This tool allows AgoraAgent to dynamically create new domain tools from
    GitHub repositories when a needed capability doesn't exist.

    When ``base_tools`` is provided, the newly created MCP tool is appended
    to the executor's base tool list so it persists across agent runs.

    Args:
        llm: Model to use for ToolMaker (default: gpt-5.1_2025-11-13)
        max_iterations: Maximum iterations for ToolMaker's workflow
        auto_cleanup: If True, clean up on failure (but keep on success)
        approval_mode: MAF approval mode for the tool call.
        base_tools: Shared mutable list of the executor's base tools;
            new MCP tools are appended here so they persist across runs.
        input_handler: Optional async callable ``(question, context) -> str``
            for mid-workflow user prompts. If None, prompts are auto-resolved
            without blocking (non-interactive mode).

    Returns:
        FunctionTool that can be added to AgoraAgent's tool list
    """

    # Track whether create_tool_from_repo already ran in this session
    # to prevent the rerun loop from invoking it again.
    _already_created: list[bool] = [False]

    # URL confirmation gate: the first call stores the URL and rejects
    # execution, forcing the LLM to ask the user to confirm.  The second
    # call with the same URL is allowed through.
    _pending_url: list[str] = [""]

    async def create_tool_from_repo(
        repo_url: str,
        tool_description: str,
        tool_name: Optional[str] = None,
        domain_name: Optional[str] = None,
    ) -> str:
        """
        Create a new MCP domain tool from a GitHub repository.

        Invokes the ToolMaker agent to autonomously:
        1. Explore the repository and understand its capabilities
        2. Generate an MCP domain server wrapping the desired functionality
        3. Build and test the domain in Docker
        4. Register the domain so it becomes available

        Args:
            repo_url: GitHub repository URL
            tool_description: Description of the tool capability needed
            tool_name: Optional specific name for the tool function
            domain_name: Optional name for the MCP domain

        Returns:
            String describing the result (success info or error)
        """
        # Guard: prevent the rerun loop from calling this again after the tool
        # was already created.  The AgoraAgent reruns when tools_loaded_flag is
        # set, and the LLM may call create_tool_from_repo a second time.
        if _already_created[0]:
            return (
                "A tool was already created in this session. The new tool is loaded "
                "and ready to use. Do NOT call create_tool_from_repo again. "
                "Use the newly available tool to answer the user's original question."
            )

        # ── URL confirmation gate ──────────────────────────────────────
        # First call: store the URL and reject — the LLM must ask the user
        # to confirm, then call again with the same (or corrected) URL.
        # Second call: if the URL matches what was pending, allow through.
        if not _pending_url[0]:
            # First call — store and bounce back
            _pending_url[0] = repo_url
            return (
                f"Before I can create a tool, the user must confirm the repository URL.\n"
                f"Proposed URL: {repo_url}\n\n"
                f"Ask the user to confirm this URL using a HelpResponse, e.g.:\n"
                f"'I'd like to create a tool from {repo_url}. Is this the right repository?'\n\n"
                f"After the user confirms (or provides a different URL), call "
                f"create_tool_from_repo again with the confirmed URL."
            )
        elif _pending_url[0] != repo_url:
            # URL changed — require fresh confirmation for the new URL.
            _pending_url[0] = repo_url
            return (
                f"The repository URL has changed.\n"
                f"New URL: {repo_url}\n\n"
                f"Ask the user to confirm this new URL using a HelpResponse, e.g.:\n"
                f"'The URL has changed to {repo_url}. Is this the right repository?'\n\n"
                f"After the user confirms, call create_tool_from_repo again "
                f"with the confirmed URL."
            )

        # Also check if a tool with this domain name is already loaded
        effective_domain = domain_name or ""
        _loaded_names = {getattr(t, "name", "") for t in (base_tools or [])}
        if effective_domain and effective_domain in _loaded_names:
            return (
                f"A tool for domain '{effective_domain}' is already loaded. "
                f"Do NOT call create_tool_from_repo again. "
                f"Use the existing tool to answer the user's question."
            )

        # Check if a matching server already exists in the MCP server registry
        # or server_registry.yaml (i.e., the tool was registered as "reusable"
        # in a previous session).  If found, auto-load it directly rather than
        # recreating — get_tool may not find it if domain_registry.yaml is
        # incomplete, so we handle the loading here.
        search_text = f"{repo_url} {tool_description} {effective_domain}".lower()
        existing_server_name = _find_existing_server(search_text, effective_domain)

        if existing_server_name:
            loaded = _try_load_existing_server(
                existing_server_name,
                base_tools=base_tools,
            )
            if loaded:
                _already_created[0] = True
                return (
                    f"A tool for '{existing_server_name}' was already registered from a "
                    f"previous session. It has been loaded and is now available. "
                    f"Do NOT call create_tool_from_repo again. "
                    f"Use the '{existing_server_name}' tool to answer the user's question."
                )
            # Server exists but isn't running — tell the LLM to ask the user
            # rather than silently recreating.
            return (
                f"A server named '{existing_server_name}' was previously registered but "
                f"is not currently running. Ask the user whether they want to recreate "
                f"the tool (you will need to confirm the GitHub URL with them first) "
                f"or start the existing server. "
                f"Do NOT call create_tool_from_repo again without user confirmation."
            )

        from toolmaker import ToolMakerAgent

        LOGGER.info(f"Creating tool from repo: {repo_url}")
        LOGGER.info(f"Tool description: {tool_description}")

        # Build the prompt for ToolMaker
        prompt_parts = [f"I want to create a tool from the GitHub repository {repo_url}."]

        if tool_name:
            prompt_parts.append(f"The tool should be named '{tool_name}'.")

        if domain_name:
            prompt_parts.append(f"The domain should be named '{domain_name}'.")

        prompt_parts.append(f"Tool capability: {tool_description}")

        prompt = " ".join(prompt_parts)

        # Let the full ToolMaker workflow run so the user gets asked
        # whether to make the tool reusable or session-only (Phase 3).
        # If reusable, Phase 4 handles registration automatically.
        toolmaker = ToolMakerAgent(
            llm=llm,
            max_iterations=max_iterations,
        )

        # Use the caller's input_handler if provided; otherwise fall back
        # to a non-interactive handler that auto-resolves prompts so the
        # tool never blocks waiting for console input.
        effective_handler = input_handler or _non_interactive_handler

        result = CreateToolResult(success=False)

        try:
            async with toolmaker:
                _ = await toolmaker.go(prompt, input_handler=effective_handler)

                # Extract information from the final state
                task_spec = toolmaker.task_spec
                impl_state = toolmaker.impl_state

                # Check if we have a working implementation
                if impl_state.tests_passed:
                    server_url = impl_state.server_url or f"http://localhost:{impl_state.server_port}/mcp"

                    # Build a human-readable test summary
                    test_lines = []
                    for i, tr in enumerate(impl_state.test_results, 1):
                        status = "PASS ✓" if tr.success else "FAIL ✗"
                        if tr.validation_passed is True:
                            status += " (validated)"
                        test_lines.append(f"    [{i}] {tr.tool_name}({tr.arguments}) → {status}")
                        if tr.output:
                            test_lines.append(f"        Output: {tr.output[:200]}")
                    test_summary = "\n".join(test_lines) if test_lines else ""

                    result = CreateToolResult(
                        success=True,
                        domain_name=task_spec.domain_name,
                        tool_name=task_spec.tool_name,
                        tool_description=task_spec.task_description,
                        server_url=server_url,
                        server_port=impl_state.server_port,
                        message=f"Tool '{task_spec.tool_name}' created successfully from {repo_url}",
                        test_summary=test_summary,
                    )
                    LOGGER.info(f"ToolMaker succeeded: {result.message}")

                    _already_created[0] = True

                    # Load the MCP tool into the executor's base tools so
                    # it persists across agent runs within this session.
                    from toolmaker.models import ToolPersistence

                    if base_tools is not None and impl_state.persistence != ToolPersistence.UNDECIDED:
                        _load_new_mcp_tool(
                            server_url=server_url,
                            domain_name=task_spec.domain_name,
                            tool_description=task_spec.task_description,
                            base_tools=base_tools,
                        )
                else:
                    # Check for partial success
                    if impl_state.image_built:
                        error_msg = "Tool built but tests did not pass"
                        if impl_state.test_results:
                            last_test = impl_state.test_results[-1]
                            if last_test.error:
                                error_msg += f": {last_test.error}"
                            elif last_test.output:
                                error_msg += f": {last_test.output}"
                        if impl_state.problem_summaries:
                            error_msg += f". History: {impl_state.problem_summaries[-1]}"
                    else:
                        error_msg = "Failed to build the tool"
                        if impl_state.build_error:
                            error_msg += f": {impl_state.build_error}"
                        elif impl_state.problem_summaries:
                            error_msg += f": {impl_state.problem_summaries[-1]}"
                        else:
                            error_msg += (
                                f" (build_status={impl_state.build_status.value}, "
                                f"iterations={impl_state.iteration}/{impl_state.max_iterations}, "
                                f"test_results={len(impl_state.test_results)})"
                            )

                    result = CreateToolResult(
                        success=False,
                        domain_name=task_spec.domain_name,
                        tool_name=task_spec.tool_name,
                        error=error_msg,
                    )
                    LOGGER.warning(f"ToolMaker failed: {result.error}")
                    USER_LOGGER.error(
                        "Tool creation failed for '%s' (domain: %s): %s",
                        task_spec.tool_name,
                        task_spec.domain_name,
                        error_msg,
                    )

        except BaseException as e:
            error_msg = f"{type(e).__name__}: {e}"
            result = CreateToolResult(
                success=False,
                error=error_msg,
            )
            LOGGER.error(f"ToolMaker error: {error_msg}", exc_info=True)
            USER_LOGGER.error("Tool creation error: %s", error_msg)

        return result.to_string()

    return FunctionTool(
        name="create_tool_from_repo",
        description=(
            "Create a new domain-specific MCP tool from a GitHub repository. "
            "PREREQUISITE: You MUST confirm the repository URL with the user via "
            "HelpResponse BEFORE calling this tool. Do NOT call this tool until the "
            "user has explicitly approved the URL. "
            "ONLY use this when search_tools cannot find an existing tool that matches the need. "
            "Invokes ToolMaker to autonomously explore the repo, generate an MCP server, "
            "build and test it in Docker, and make it available as a new tool. "
            "The user will be asked whether to make it reusable or session-only. "
            "After success, the new tool is loaded automatically — do NOT call search_tools. "
            "Use the new tool immediately to answer the user's original question. "
            "IMPORTANT: If this tool returns an error, you MUST use a HelpResponse to ask the user for help. "
            "Do NOT use a SolutionResponse and do NOT silently fall back to answering from your own knowledge when tool creation fails."
        ),
        approval_mode=approval_mode,
        func=create_tool_from_repo,
        input_model=CreateToolInput,
    )


def _load_new_mcp_tool(
    server_url: str,
    domain_name: str,
    tool_description: str,
    base_tools: list,
) -> None:
    """Create an MCPStreamableHTTPTool for the new server and add it to base_tools.

    Appends the tool to the executor's ``base_tools`` list so it persists
    across agent runs within the current session.
    """
    mcp_server_scope = os.getenv("MCP_SERVER_SCOPE")
    if not mcp_server_scope:
        LOGGER.warning("MCP_SERVER_SCOPE not set — cannot create MCP tool for new server")
        return

    try:
        token_provider = create_entra_token_provider(mcp_server_scope)
        http_client = httpx.AsyncClient(
            auth=BearerTokenAuth(token_provider),
            timeout=httpx.Timeout(connect=5.0, write=10.0, read=None, pool=5.0),
        )
        mcp_tool = MCPStreamableHTTPTool(
            url=server_url,
            name=domain_name,
            description=tool_description,
            http_client=http_client,
        )

        base_tools.append(mcp_tool)

        LOGGER.info(f"Loaded new MCP tool '{domain_name}' into base_tools from {server_url}")
    except Exception as e:
        LOGGER.error(f"Failed to load new MCP tool '{domain_name}': {e}", exc_info=True)


def _find_existing_server(search_text: str, effective_domain: str) -> Optional[str]:
    """Check if a matching MCP server already exists.

    Searches both the runtime MCP server registry and ``server_registry.yaml``
    for a server whose name appears in *search_text* or matches *effective_domain*.

    Returns:
        The server name if found, otherwise ``None``.
    """
    # 1) Check runtime MCP registry
    try:
        from tools.mcp.mcp_server_registry import get_mcp_registry

        mcp_registry = get_mcp_registry()

        if effective_domain and mcp_registry.has_server(effective_domain):
            return effective_domain

        for name in mcp_registry.list_servers():
            if name.lower() in search_text:
                return name
    except Exception:
        LOGGER.debug("MCP registry lookup failed; falling back to server_registry.yaml", exc_info=True)

    # 2) Check server_registry.yaml (persists even when servers are stopped)
    try:
        from pathlib import Path
        import yaml

        yaml_path = Path(__file__).resolve().parent.parent.parent / "server_registry.yaml"
        if yaml_path.exists():
            with open(yaml_path) as f:
                data = yaml.safe_load(f) or {}
            for srv in data.get("servers", []):
                srv_name = srv.get("name", "").lower()
                if srv_name and srv_name in search_text:
                    return srv.get("name", "")
    except Exception:
        LOGGER.debug("server_registry.yaml lookup failed", exc_info=True)

    return None


def _try_load_existing_server(
    server_name: str,
    base_tools: Optional[list],
) -> bool:
    """Try to load an existing MCP server tool into the executor's base_tools.

    Reads the server URL from the MCP registry or ``server_registry.yaml``
    and calls :func:`_load_new_mcp_tool`.

    Returns:
        ``True`` if the tool was loaded, ``False`` otherwise.
    """
    if base_tools is None:
        return False

    # Try to find the server URL from runtime registry first
    server_url: Optional[str] = None
    description = f"Domain tool: {server_name}"
    try:
        from tools.mcp.mcp_server_registry import get_mcp_registry

        mcp_registry = get_mcp_registry()
        descriptor = mcp_registry.get(server_name)
        if descriptor:
            server_url = descriptor.url
            description = descriptor.description
    except Exception:
        LOGGER.debug("MCP registry lookup failed for '%s'", server_name, exc_info=True)

    # Fall back to server_registry.yaml
    if not server_url:
        try:
            from pathlib import Path
            import yaml

            yaml_path = Path(__file__).resolve().parent.parent.parent / "server_registry.yaml"
            if yaml_path.exists():
                with open(yaml_path) as f:
                    data = yaml.safe_load(f) or {}
                base_url = data.get("base_url", "http://localhost")
                for srv in data.get("servers", []):
                    if srv.get("name", "").lower() == server_name.lower():
                        port = srv.get("port", 8010)
                        server_url = f"{base_url}:{port}/mcp"
                        break
        except Exception:
            LOGGER.debug("server_registry.yaml lookup failed for '%s'", server_name, exc_info=True)

    if not server_url:
        LOGGER.warning(f"Could not determine URL for server '{server_name}'")
        return False

    try:
        _load_new_mcp_tool(
            server_url=server_url,
            domain_name=server_name,
            tool_description=description,
            base_tools=base_tools,
        )
        LOGGER.info(f"Auto-loaded existing server '{server_name}' from {server_url}")
        return True
    except Exception as e:
        LOGGER.warning(f"Failed to auto-load existing server '{server_name}': {e}")
        return False
