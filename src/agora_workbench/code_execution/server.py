"""
Base Code Execution Server for MCP.

This module provides a base class for creating MCP servers that execute
Python code in isolated environments with domain-specific packages.
"""

import asyncio
import inspect
import json
import keyword
import logging
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

from fastapi import HTTPException
from fastmcp import Context, FastMCP
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from . import agent_guidance
from . import environment_builders
from . import asset_provisioner
from . import code_execution as execution_defaults
from .sidecar import SidecarManager
from .code_execution_models import (
    CodeExecutionResult,
    ServerConfig,
    ToolCallRecord,
)
from .auth.base import AuthConfig
from agora_workbench.base import BaseMCPServer
from .data_access import AssetResolutionMiddleware
from .sessions import (
    MaxSessionsReachedError,
    SessionConfig,
    SessionManager,
    SessionNotFound,
    get_current_session,
    set_current_session,
    get_current_request_token,
    set_current_request_token,
    get_current_user_identity,
    set_current_user_identity,
    get_current_token_claims,
    set_current_token_claims,
    register_session_meta_tools,
)
from .tool_proxy import (
    generate_tracing_infrastructure_code,
    generate_tool_proxies,
    generate_list_tools_code,
    FLUSH_SNIPPET,
)

if TYPE_CHECKING:
    from .data_access.publishers import AssetPublisher
    from .sessions import Session
    from .skills import Skill
    from .tool_registry import State, ToolRegistry
    from .tools.tool_search import ToolSearchBackend

# Configure logging
logging.basicConfig(level=logging.INFO)
logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.WARNING)
logging.getLogger("azure.identity").setLevel(logging.WARNING)
LOGGER = logging.getLogger(__name__)


class CodeExecutionServer(BaseMCPServer):
    """
    Base class for MCP code execution servers.

    Subclass this to create execution environments with specific Python
    interpreters and package sets. Each server exposes an MCP tool for
    executing code in its isolated environment.

    Security features:
    - Timeout enforcement
    - Separate process execution
    - Optional working directory isolation
    - Configurable resource limits (override in subclass)
    - Pluggable authentication (Entra ID, no-op/dev, or custom)
    - Managed identity for downstream Azure resource access

    Middleware Architecture:
    The server uses two layers of middleware at different levels of the stack:

    **Starlette Middleware (ASGI level, outermost layer):**
    Applied to the entire HTTP application before FastMCP processing. Added in
    _create_middleware() and registered via app.add_middleware() during serve().

    1. MCPSessionMiddleware (outermost):
       - Extracts Mcp-Session-Id header from requests to /mcp endpoint
       - Stores session ID in ASGI scope for downstream access
       - Enables session correlation across tool calls

    2. AuthMiddleware (inner):
       - Validates Bearer tokens on /mcp and /object-transfer/* endpoints using Entra ID
       - Extracts user identity (oid/sub) and token claims
       - Stores authenticated context (token, claims, user_identity)
       - Bypasses /health and /.well-known/* endpoints (no auth required)
       - Returns RFC 9728 WWW-Authenticate header on 401 for OAuth discovery

    **FastMCP Middleware (tool call level, innermost layer):**
    Applied to MCP tool calls after HTTP auth but before Pydantic validation.
    Added directly to the FastMCP instance:

    1. AssetResolutionMiddleware (added during __init__):
       - Resolves tagged asset references like <blob>id</blob> to local cache paths
       - Runs BEFORE Pydantic validation so Path parameters receive proper values

    Full execution flow:
    HTTP Request → MCPSessionMiddleware → AuthMiddleware → FastMCP routing →
    AssetResolutionMiddleware → Pydantic validation → Tool callback

    Constraints:
    - Asset resolution happens first, so asset parameters are properly typed

    Asset/Handle Injection Patterns:
    The server supports two different patterns for injecting assets and handles:

    1. **Auto-Extraction (execute_code_tool)**:
       - Handle IDs (h_xxxxxxxxxxxx) and asset tags (<type>id</type>) embedded as
         string literals in code are automatically detected via AST analysis.
       - Matched literals are replaced with synthetic variables that hold the
         resolved object (for handles) or a Path to the cached file (for assets).
       - Validation: References must appear as complete string literals in
         assignments, function arguments, return statements, or container
         literals (list/dict/tuple/set). Unresolvable references fail fast.

    2. **Middleware-Based Injection (domain tools)**:
       - Assets embedded in natural parameter values: grid_file="<blob>xyz</blob>"
       - Middleware extracts, validates, and resolves before Pydantic validation
       - Use case: LLM-driven tool calls where assets are encoded in argument values
       - Validation: AssetResolutionMiddleware
    """

    def __init__(
        self,
        server_config: ServerConfig,
        tool_registry: Optional["ToolRegistry"] = None,
        session_manager: Optional["SessionManager"] = None,
        auth_config: Optional["AuthConfig"] = None,
        working_dir: Optional[Path] = None,
        tool_search_backend: Optional["ToolSearchBackend"] = None,
        publishers: "Optional[list[AssetPublisher]]" = None,
        skills: "Optional[list[Skill]]" = None,
        states: "Optional[list[State]]" = None,
    ):
        """
        Initialize the code execution server.

        Args:
            server_config: Server configuration (environment, assets, execution policy, features)
            tool_registry: Optional ToolRegistry containing domain-specific tools
            session_manager: Optional SessionManager for stateful tool support (auto-created with defaults if None)
            auth_config: Authentication configuration providing token validation,
                identity extraction, and credential provisioning.
            working_dir: Working directory for code execution (None = temp dir per execution)
            tool_search_backend: Optional pre-configured ToolSearchBackend instance.
                If provided, this backend is used instead of creating one from config.
                Enables custom search backends (e.g. vector DB, Elasticsearch) without
                modifying the built-in factory.
            publishers: Optional list of :class:`~.data_access.AssetPublisher` instances
                that the ``{name}_send`` MCP tool will dispatch to.
                Publishers are routed by ``destination_name``; the first match wins.
                A :class:`~.data_access.GuiPublisher` is always prepended (see
                ``__init__``) so the send tool is registered unconditionally and
                the ``"user"`` destination works even when this list is ``None``.
            skills: Optional list of :class:`~.skills.Skill` objects for workflow planning
                and skill loading. Use :func:`~.skills.discover_skills` to scan a directory.
            states: Optional list of :class:`~.tool_registry.State` objects defining the
                domain's state vocabulary with descriptions and affordances.
        """
        super().__init__()
        self.server_config = server_config
        self.tool_registry = tool_registry

        # Sidecar processes (e.g. a shared model service). Lazily started in
        # _startup and stopped in _shutdown; no-op when none are declared.
        self._sidecar_manager = SidecarManager(server_config)

        self.skills: list["Skill"] = list(skills or [])
        self.states: list["State"] = list(states or [])
        self._state_affordances: dict[str, list[str]] = {s.token: s.affordances for s in self.states if s.affordances}
        self._tool_proxies_injected: set[str] = set()
        self._tool_search_backends: list[Any] = []
        self._custom_tool_search_backend = tool_search_backend
        self._publishers: "list[AssetPublisher]" = list(publishers or [])
        self._parallel_jobs: dict[str, dict[str, Any]] = {}
        self._parallel_batches: dict[str, dict[str, Any]] = {}
        self._parallel_job_by_session: dict[str, str] = {}
        self._parallel_state_lock = asyncio.Lock()

        # GuiPublisher is always available so agents can use <gui>name</gui>
        # to make outputs downloadable without requiring external storage.
        from .data_access.publishers import GuiPublisher

        self._gui_publisher = GuiPublisher(public_url_fn=self.public_url)
        self._publishers.insert(0, self._gui_publisher)

        # Shared peer registry: name → base URL of peer servers reachable via
        # {name}_send(to=<peer>). Resolved once so the send tool can construct a
        # ServerPublisher on demand instead of requiring one pre-registered per peer.
        self._peer_registry: dict[str, str] = self._load_peer_registry()
        # Resolution: ServerConfig overrides env var; env var provides deployment default.
        if server_config.parallel_max_concurrency is not None:
            parallel_execute_max_concurrency = server_config.parallel_max_concurrency
        else:
            parallel_execute_max_concurrency_raw = os.getenv("PARALLEL_EXECUTE_MAX_CONCURRENCY", "0").strip()
            try:
                parallel_execute_max_concurrency = int(parallel_execute_max_concurrency_raw)
            except ValueError:
                LOGGER.warning(
                    "Invalid PARALLEL_EXECUTE_MAX_CONCURRENCY value %r; using default 0.",
                    parallel_execute_max_concurrency_raw,
                )
                parallel_execute_max_concurrency = 0
        self.parallel_max_concurrency = max(0, parallel_execute_max_concurrency)
        self._parallel_semaphore: Optional[asyncio.Semaphore] = (
            asyncio.Semaphore(self.parallel_max_concurrency) if self.parallel_max_concurrency > 0 else None
        )

        # Auto-create session manager with defaults if not provided
        if session_manager is None:
            _session_manager = SessionManager(SessionConfig())
            LOGGER.info("Created default SessionManager.")
        else:
            _session_manager = session_manager

        self.session_manager = _session_manager

        # --- Authentication configuration ---
        if auth_config is None:
            raise ValueError(
                "auth_config is required. Use create_entra_auth_config() for Entra ID "
                "or create_noop_auth_config() for development."
            )
        self.auth_config = auth_config

        # Entra client/tenant IDs for RFC 9728 OAuth protected-resource metadata.
        # Preferred source is auth_config.protected_resource_metadata; these attributes
        # remain for back-compat and for deployments that configure Entra IDs directly.
        # Resolution order: auth_config validator → ServerConfig → environment variable.
        #
        # Reading private validator attributes is a deprecated legacy convention, kept
        # for third-party validators written against it. Probed symmetrically so a
        # validator exposing only one of the two cannot raise or be partially ignored.
        self.entra_client_id: Optional[str] = getattr(auth_config.token_validator, "_client_id", None)
        self.entra_tenant_id: Optional[str] = getattr(auth_config.token_validator, "_tenant_id", None)
        if not self.entra_client_id:
            self.entra_client_id = server_config.entra_client_id or os.getenv("ENTRA_CLIENT_ID")
        if not self.entra_tenant_id:
            self.entra_tenant_id = server_config.entra_tenant_id or os.getenv("ENTRA_TENANT_ID")

        self._warn_if_oauth_metadata_unresolvable()

        self.max_timeout = server_config.max_timeout
        self.default_timeout = server_config.default_timeout

        # Resolution: ServerConfig overrides env var; env var provides deployment default.
        if server_config.output_truncation_threshold is not None:
            self.output_truncation_threshold = server_config.output_truncation_threshold
        else:
            env_threshold = os.getenv("CODE_OUTPUT_TRUNCATION_THRESHOLD", "50000")
            normalized = env_threshold.strip().replace("_", "")
            try:
                parsed = int(normalized)
                if parsed < 0:
                    raise ValueError("threshold must be >= 0")
                self.output_truncation_threshold = parsed
            except ValueError:
                LOGGER.warning(
                    "Invalid CODE_OUTPUT_TRUNCATION_THRESHOLD=%r; using default 50000",
                    env_threshold,
                )
                self.output_truncation_threshold = 50_000

        self.working_dir = working_dir
        self._python_executable: Optional[Path] = None
        self._environment_ready = False

        # Best-effort activity publisher (silent no-op when ACTIVITY_UI_URL is unset).
        from .activity_publisher import ActivityPublisher

        self.activity_publisher = ActivityPublisher(server_name=server_config.name)

        self.mcp = FastMCP(
            f"{server_config.name}-executor",
            instructions=server_config.server_description or server_config.description,
        )

        # AssetResolutionMiddleware resolves tagged asset references before Pydantic validation.
        self.mcp.add_middleware(AssetResolutionMiddleware(self))

        self._setup_tools()

    # ========================================================================
    # Environment Building
    # ========================================================================

    async def _ensure_environment(self):
        """Ensure the Python environment exists, build if necessary."""
        if self._environment_ready:
            return

        config = self.server_config

        # Check if environment already exists
        expected_python = config.get_python_path()
        if expected_python.exists():
            self._python_executable = expected_python
            self._environment_ready = True
            LOGGER.info(f"Found existing environment: {self._python_executable}")
        elif config.auto_build:
            # Build environment if auto_build is enabled
            LOGGER.info(f"Building {config.type} environment: {config.name}")
            await self._build_environment(config)
            self._python_executable = config.get_python_path()
            self._environment_ready = True
            LOGGER.info(f"Environment built successfully: {self._python_executable}")
        else:
            raise RuntimeError(
                f"Python environment not found at {expected_python} and auto_build is disabled. "
                f"Either build the environment manually or set auto_build=True in ServerConfig."
            )

        # Provision large assets (model weights, data files) after env is ready
        if config.assets and config.auto_provision:
            await asset_provisioner.provision_assets(config)

    async def _build_environment(self, config: ServerConfig):
        """Build the Python environment based on config."""
        build_dir = config.get_build_dir()

        # Write dependency file to the parent directory
        parent_dir = build_dir.parent
        parent_dir.mkdir(parents=True, exist_ok=True)

        # Write dependency file content to disk
        if config.type == "conda":
            filename = "environment.yml"
        elif config.type == "uv":
            filename = "requirements.txt"
        elif config.type == "pip":
            filename = "requirements.txt"
        else:
            filename = "requirements.txt"

        dep_file_path = parent_dir / filename
        dep_file_path.write_text(config.dependency_file)
        LOGGER.info(f"Wrote dependency file to {dep_file_path}")

        LOGGER.info(f"Building {config.type} environment in {build_dir}")

        if config.type == "uv":
            await self._build_uv_environment(config)
        elif config.type == "conda":
            await self._build_conda_environment(config)
        elif config.type == "pip":
            await self._build_pip_environment(config)
        else:
            raise ValueError(f"Unsupported environment type: {config.type}")

    async def _build_uv_environment(self, config: ServerConfig):
        """Build environment using uv."""
        await environment_builders.build_uv_environment(config)

    async def _build_conda_environment(self, config: ServerConfig):
        """Build environment using conda."""
        await environment_builders.build_conda_environment(config)

    async def _build_pip_environment(self, config: ServerConfig):
        """Build environment using Python venv + pip."""
        await environment_builders.build_pip_environment(config)

    async def warm(self) -> None:
        """Pre-initialize the execution environment without serving requests.

        Call this during Docker builds or process startup to avoid cold-start
        latency. It prepares the Python environment, provisions assets according
        to the ServerConfig (e.g. when auto_provision is enabled), and registers
        the execution kernel.
        """
        LOGGER.info(f"Warming environment: {self.server_config.name}")
        await self._ensure_environment()
        await self._register_kernel(kernel_name="tools-py")
        LOGGER.info(f"✓ Environment '{self.server_config.name}' is warm and ready.")

    def main(self, *, default_host: str = "0.0.0.0", default_port: int = 8000) -> None:
        """CLI entrypoint that handles ``--warm``, ``--host``, and ``--port``.

        Call this from your server's ``if __name__ == "__main__"`` block to get
        standard flag handling without manual ``sys.argv`` parsing::

            server = MyDomainServer(...)
            server.main()

        Flags:
            --warm          Pre-initialize the environment and exit (no HTTP server).
            --host HOST     Bind address (default: default_host, or HOST env var).
            --port PORT     Bind port (default: default_port, or PORT env var).
        """
        import argparse

        parser = argparse.ArgumentParser(
            description=f"{self.server_config.name} — CodeExecutionServer",
        )
        parser.add_argument(
            "--warm",
            action="store_true",
            help="Pre-initialize the environment and exit without starting the server.",
        )
        env_host = os.getenv("HOST")
        env_port = os.getenv("PORT")
        try:
            port_default = int(env_port) if env_port else default_port
        except ValueError:
            parser.error(f"Invalid PORT env var: {env_port!r} (must be an integer).")

        parser.add_argument(
            "--host",
            default=env_host or default_host,
            help=f"Bind address (default: {default_host}, or HOST env var).",
        )
        parser.add_argument(
            "--port",
            type=int,
            default=port_default,
            help=f"Bind port (default: {default_port}, or PORT env var).",
        )
        args = parser.parse_args()

        if args.warm:
            asyncio.run(self.warm())
        else:
            asyncio.run(self.run_http(host=args.host, port=args.port))

    # ========================================================================
    # Optional hooks - can be overridden by subclasses
    # ========================================================================

    def get_tool_name(self) -> str:
        """Get the MCP tool name. Override to customize."""
        return f"execute_{self.server_config.name}_code"

    def public_url(self) -> str:
        """Best-effort base URL for outbound references (e.g. download links).

        Falls back to ``http://localhost:<port>`` when called before
        :meth:`run_http` has stashed the bind host/port.  Callers that need
        the real externally-visible URL (containerized deployments behind a
        proxy / Ingress) should set ``SERVER_PUBLIC_URL`` instead — that env
        var takes precedence wherever this method is consulted.
        """
        host = getattr(self, "_bind_host", None) or "localhost"
        port = getattr(self, "_bind_port", None) or 8000
        # 0.0.0.0 is a bind address, not a reachable one.  Map it back to
        # localhost for outward-facing URLs; for non-localhost deployments
        # the operator should set SERVER_PUBLIC_URL explicitly.
        if host == "0.0.0.0":
            host = "localhost"
        return f"http://{host}:{port}"

    def preprocess_code(self, code: str) -> str:
        """
        Preprocess code before execution.

        Override to add imports, setup code, or modify the input.
        """
        return code

    # blacklists for default code validation tool
    _BLOCKED_FS_CALLS = execution_defaults._BLOCKED_FS_CALLS
    _BLOCKED_DANGEROUS_CALLS = execution_defaults._BLOCKED_DANGEROUS_CALLS
    _BLOCKED_MODULES = execution_defaults._BLOCKED_MODULES
    _ALLOWED_PATH_PREFIXES = execution_defaults._ALLOWED_PATH_PREFIXES

    def validate_code(self, code: str) -> tuple[bool, "Optional[str]"]:
        """
        Validate user-provided code before execution.
        This is a thin instance-method wrapper around the default implementation
        in `execution_defaults.validate_code`, which expects `self` as its first
        argument.

        Override to adjust behavior.
        """
        return execution_defaults.validate_code(self, code)

    def postprocess_result(self, result: CodeExecutionResult) -> CodeExecutionResult:
        """
        Postprocess execution result.

        Override to filter output, add metadata, or modify the result.
        """
        return result

    def _truncate_output_if_needed(self, result: CodeExecutionResult) -> CodeExecutionResult:
        """Truncate stdout/stderr if they exceed ``output_truncation_threshold``.

        When either stream is larger than the configured threshold, the excess
        content is removed and a guidance notice is appended that instructs the
        LLM to inspect large objects server-side (e.g. via targeted print
        statements or variable inspection) rather than pulling the full output
        through the MCP interface into the agent context.

        Args:
            result: The execution result to potentially truncate.

        Returns:
            The original result unchanged when both streams are within the
            threshold, or a new result with truncated streams and appended
            guidance notices.
        """
        threshold = self.output_truncation_threshold
        if threshold <= 0:
            return result

        _TRUNCATION_GUIDANCE = (
            " Store large results in variables and use targeted inspection "
            "(e.g. ``print(result[:200])``, ``len(result)``, ``type(result)``) "
            "to examine objects server-side without transferring the full output "
            "through the MCP interface."
        )

        new_stdout = result.stdout
        new_stderr = result.stderr

        if len(result.stdout) > threshold:
            notice = (
                f"\n[OUTPUT TRUNCATED: {len(result.stdout):,} characters exceeded the "
                f"{threshold:,} character limit.{_TRUNCATION_GUIDANCE}]"
            )
            new_stdout = result.stdout[:threshold] + notice
            LOGGER.info(
                "stdout truncated from %d to %d characters (threshold=%d)",
                len(result.stdout),
                len(new_stdout),
                threshold,
            )

        if len(result.stderr) > threshold:
            # Keep head + tail for stderr so Python tracebacks (which appear at
            # the end) remain visible after truncation.
            half = threshold // 2
            head = result.stderr[:half]
            tail = result.stderr[-half:] if half > 0 else ""
            notice = (
                f"\n[STDERR TRUNCATED: {len(result.stderr):,} characters exceeded the "
                f"{threshold:,} character limit.{_TRUNCATION_GUIDANCE}]\n"
            )
            new_stderr = head + notice + tail
            LOGGER.info(
                "stderr truncated from %d to %d characters (threshold=%d)",
                len(result.stderr),
                len(new_stderr),
                threshold,
            )

        if new_stdout is result.stdout and new_stderr is result.stderr:
            return result

        return result.model_copy(update={"stdout": new_stdout, "stderr": new_stderr})

    # ========================================================================
    # Authentication helper methods
    # ========================================================================

    def _extract_user_identity(self, token_data: dict) -> Optional[str]:
        """
        Extract a unique user identity string from validated token claims.

        Delegates to ``self.auth_config.identity_extractor``.

        Args:
            token_data: Decoded JWT token claims dict.

        Returns:
            A unique identity string, or ``None`` when required claims are missing.
        """
        return self.auth_config.identity_extractor.extract(token_data)

    def _clear_auth_context(self):
        """
        Clear authentication context variables after request completes.

        This clears the token, claims, user identity, and MCP session ID from
        context vars to prevent leakage between requests. Called after tool
        execution completes (in finally blocks).
        """
        set_current_request_token(None)
        set_current_token_claims(None)
        set_current_user_identity(None)

    def _restore_auth_context_for_mcp_session(self, session_id: Optional[str]):
        """
        Restore authentication context from a stored MCP session.

        StreamableHTTP processes tool executions in background tasks that don't
        inherit the ContextVars from the original HTTP request handler. This method
        restores the auth context (token, claims, user identity) from the session
        that was created during the initial connection.

        Args:
            session_id: The MCP transport session ID to restore context from.
                       If None or session not found, context is not modified.
        """
        if not session_id:
            LOGGER.debug("No session_id provided, skipping auth context restore")
            return

        # Check if context is already set (e.g., in same task as auth middleware)
        if get_current_user_identity():
            LOGGER.debug("Auth context already set, skipping restore")
            return

        try:
            session = self.session_manager.get_session(session_id)
            # Restore auth context from session
            set_current_user_identity(session.user_identity)
            set_current_request_token(session.user_token)
            # Restore cached token claims only if they have a valid, non-expired exp claim
            token_claims = getattr(session, "token_claims", None)
            if isinstance(token_claims, dict):
                exp = token_claims.get("exp")
                # Only trust cached claims if they have a valid, non-expired exp claim
                if isinstance(exp, (int, float)) and exp > time.time():
                    set_current_token_claims(token_claims)
                else:
                    LOGGER.warning(
                        f"Cached token claims for session {session_id[:8]} have missing or expired 'exp'; "
                        "skipping claims restore. Token will be re-validated if needed."
                    )
            LOGGER.debug(f"Restored auth context from session {session_id[:8]}")
        except Exception as e:
            # Session not found or other error - this is expected for new sessions
            LOGGER.debug(f"Could not restore auth context from session {session_id}: {e}")

    # ========================================================================
    # MCP Tool Setup
    # ========================================================================

    def _setup_code_execution_tool(self) -> None:
        """
        Set up the code execution tool for this server instance.
        Delegates to `execution_defaults.setup_code_execution_tool`, passing
        this instance as the first argument.
        """
        # Build code execution tool
        execute_code_tool = execution_defaults.build_tool(self)

        # Build tool description from the domain config description + the function docstring,
        # resolving the {max_timeout} placeholder.
        tool_catalog = self._build_tool_catalog_summary()
        tool_description = (
            self.server_config.description
            + "\n\n"
            + inspect.cleandoc(execute_code_tool.__doc__ or "").format(max_timeout=self.max_timeout)
            + "\n\n"
            + execution_defaults._build_disallowed_actions_description(self)
        )
        if tool_catalog:
            tool_description += "\n\n" + tool_catalog
        self.mcp.tool(name=self.get_tool_name(), description=tool_description)(execute_code_tool)

        check_job_tool = execution_defaults.build_check_job_tool(self)
        self.mcp.tool(
            name=f"{self.server_config.name}_check_job",
            description=(
                "Check status/output for a background code-execution job. "
                "Jobs are created when execution_mode is 'async_only' or when "
                "'adaptive' mode promotes a long-running execution to background."
            ),
        )(check_job_tool)

    def _setup_tools(self) -> None:
        """Set up MCP tools using FastMCP decorators."""
        # Setup general code execution tool
        self._setup_code_execution_tool()

        # Register server-side search (search_{name}_tools) and workflow/skill
        # tools (plan_{name}_workflow, load_{name}_skill).  These remain useful
        # for skill-only / wrapper-less BYOA servers (e.g. earthscience), so
        # they are not gated on tool_registry.  Each helper guards internally.
        self._setup_search_tool()
        self._setup_workflow_planning_tools()

        # Setup session management meta tools (prefixed with server name for uniqueness)
        register_session_meta_tools(
            self.mcp,
            self.session_manager,
            name_prefix=self.server_config.name,
            inspector=self._inspect_session_payload,
        )

        # Setup batch-parallel execution tools.
        self._setup_parallel_execution_tools()

        # Setup unified send tool (replaces push_object + publish_artifact)
        self._setup_send_tool()

    # ========================================================================
    # Session Management Helpers
    # ========================================================================

    async def _verify_session_ownership(self, session: "Session", request_token: Optional[str]) -> bool:
        """
        Verify that the caller is authorized to access this session.

        Security check to prevent session hijacking. Validates that:
        1. Request has a valid authentication token
        2. Token contains matching user identity for session (``oid@tid``)

        Sessions are user-owned resources: any caller presenting a valid token
        for this API with a user identity (``oid@tid``) that matches the session
        owner is authorized.  Environment-level isolation (unique
        ``ENTRA_CLIENT_ID``/``ENTRA_TENANT_ID`` per deployment) provides the
        boundary between environments.

        Args:
            session: Session to validate access for
            request_token: Authentication token from current request

        Returns:
            True if caller is authorized, False otherwise
        """
        if request_token is None:
            LOGGER.warning("Session access attempt without authentication token")
            return False

        try:
            # Get cached token claims from context (already validated by AuthMiddleware)
            token_data = get_current_token_claims()
            if not token_data:
                # No cached claims - validate token now (e.g., in tests or direct calls)
                LOGGER.debug("Token claims not in context, validating token for ownership check")
                token_data = await self.validate_token(request_token)
            else:
                LOGGER.debug(f"Using cached token claims for ownership check (keys: {list(token_data.keys())})")

            # caller_app_id is captured for logging purposes only; app-level
            # allowlisting is not enforced — authorization is based solely on
            # token validity and matching user identity (oid@tid).
            caller_app_id = token_data.get("appid") or token_data.get("azp")

            # Verify user identity matches session
            # Extract composite user identity (oid@tid) from token
            token_user_id = self._extract_user_identity(token_data)

            if not token_user_id:
                LOGGER.warning("Token missing user identity claims (oid/sub and/or tid)")
                return False

            if token_user_id != session.user_identity:
                LOGGER.warning(
                    f"User identity mismatch: token user {token_user_id} "
                    f"doesn't match session user {session.user_identity} "
                    f"for session {session.session_id}"
                )
                return False

            LOGGER.debug(
                f"Session {session.session_id} access authorized for app {caller_app_id or 'delegated'} "
                f"(user: {session.user_identity})"
            )
            return True

        except Exception as e:
            LOGGER.error(
                f"Session ownership verification failed with {type(e).__name__}: {e}",
                exc_info=True,
            )
            return False

    @staticmethod
    def _max_sessions_error_payload(error: MaxSessionsReachedError) -> dict[str, Any]:
        """Build a typed, user-facing payload for session-capacity failures."""
        return {
            "success": False,
            "error_type": "max_sessions_reached",
            "error": str(error),
            "status_code": 429,
        }

    @staticmethod
    def _is_max_sessions_http_error(error: HTTPException) -> bool:
        """Whether an HTTPException represents the typed max-sessions failure."""
        return error.status_code == 429 and isinstance(error.detail, dict)

    @staticmethod
    def _refresh_session_token(session: "Session") -> None:
        """Update the session's stored token with the fresh token from the current request.

        Tokens issued by the identity provider expire (typically within minutes).
        The session captures a token at creation time, but subsequent requests
        carry a fresh token set by the authentication middleware in
        :func:`get_current_request_token`.  This helper propagates that fresh
        token into the session so that downstream code (object transfer, data
        access, kernel environment) always uses a valid credential.

        When the token changes the cached ``token_claims`` are also replaced
        with the claims from the current request context so that they stay
        consistent with the stored bearer token.

        Note: Since `DataLakeDataManager` uses managed identity (not OBO), it
        does not depend on the user's bearer token and is not recreated here.
        """
        fresh_token = get_current_request_token()
        if fresh_token and fresh_token != session.user_token:
            session.user_token = fresh_token
            # Keep token_claims in sync so cached claims match the new token.
            fresh_claims = get_current_token_claims()
            if fresh_claims is not None:
                session.token_claims = fresh_claims
            LOGGER.debug(f"Refreshed token for session {session.session_id[:8]}")

    async def _get_existing_session(self, session_id: str) -> "Session":
        """Load an existing execution session after validating caller ownership.

        Unlike transport-session lookup, this method never creates a session.
        It is used for explicit cross-agent reconnection, where a stale or
        invalid ID must fail instead of becoming a new empty kernel.
        """
        session = self.session_manager.get_session(session_id)
        request_token = get_current_request_token()
        if not await self._verify_session_ownership(session, request_token):
            raise PermissionError(f"Not authorized to access session {session_id}.")

        self._refresh_session_token(session)
        return session

    async def _get_or_create_session(self, tool_name: str, session_id: Optional[str] = None) -> "Session":
        """
        Get or create a session for tool execution.

        Prefers ContextVar-based session injection; otherwise auto-creates.
        User identity is extracted from the validated token in context.

        Args:
            tool_name: Name of the tool requesting the session (for logging)
            session_id: Optional session ID from transport

        Returns:
            Session object (existing or newly created)
        """
        # Get user identity from validated token claims in context
        user_identity = get_current_user_identity()
        user_token = get_current_request_token()
        token_claims = get_current_token_claims() or {}

        if not user_identity:
            raise RuntimeError(
                "No user identity in context. This indicates authentication middleware failed or was bypassed."
            )

        if user_token is None:
            raise RuntimeError(
                "No user token in context. This indicates authentication middleware failed or was bypassed."
            )

        # If transport provided a session_id, prefer it deterministically.
        if session_id:
            try:
                return await self._get_existing_session(session_id)
            except ValueError:
                try:
                    self.session_manager.create_session(
                        data={},
                        user_identity=user_identity,
                        user_token=user_token,
                        metadata={"type": "mcp_connection"},
                        session_id=session_id,
                        token_claims=token_claims,
                    )
                except MaxSessionsReachedError as e:
                    raise HTTPException(status_code=429, detail=self._max_sessions_error_payload(e)) from e
                return self.session_manager.get_session(session_id)

        # Prefer ContextVar-based session injection
        try:
            session = get_current_session()
            LOGGER.debug(f"Using session {session.session_id} from context for '{tool_name}'")
            self._refresh_session_token(session)
            return session
        except SessionNotFound:
            pass

        # No session found - client didn't provide session_id, so auto-create
        try:
            auto_session_id = self.session_manager.create_session(
                data={},
                user_identity=user_identity,
                user_token=user_token,
                metadata={"type": "auto_created", "tool": tool_name},
                token_claims=token_claims,
            )
        except MaxSessionsReachedError as e:
            raise HTTPException(status_code=429, detail=self._max_sessions_error_payload(e)) from e
        session = self.session_manager.get_session(auto_session_id)
        LOGGER.info(f"Auto-created session {auto_session_id} for '{tool_name}' (no session_id in request)")
        return session

    # ========================================================================
    # Code Execution
    # ========================================================================

    async def get_python_executable(self) -> str:
        """
        Return the path to the Python interpreter for this environment.

        This will automatically build the environment if needed and build_environment=True.
        """
        if self._python_executable and self._environment_ready:
            return str(self._python_executable)

        await self._ensure_environment()

        if not self._python_executable:
            raise RuntimeError("Failed to determine Python executable path")

        return str(self._python_executable)

    async def _register_kernel(self, kernel_name: str = "tools-py"):
        """
        Register the Python environment as a Jupyter kernel.

        Args:
            kernel_name: Name to register the kernel under
        """
        if not self._python_executable:
            raise RuntimeError("Python executable not set - build environment first")

        # Check if kernel is already registered with the correct Python executable
        kernel_dir = Path.home() / ".local" / "share" / "jupyter" / "kernels" / kernel_name
        if kernel_dir.exists():
            kernel_json = kernel_dir / "kernel.json"
            if kernel_json.exists():
                import shutil

                try:
                    spec = json.loads(kernel_json.read_text())
                    existing_python = spec.get("argv", [None])[0]
                    if existing_python == str(self._python_executable):
                        LOGGER.info(f"Kernel '{kernel_name}' already registered at {kernel_dir}")
                        return
                    LOGGER.info(
                        f"Kernel '{kernel_name}' exists but points to stale path "
                        f"({existing_python}); re-registering with {self._python_executable}"
                    )
                except (json.JSONDecodeError, IndexError):
                    LOGGER.warning(f"Kernel '{kernel_name}' has invalid spec; re-registering")

                # Remove stale spec so ipykernel install can write a fresh one
                shutil.rmtree(kernel_dir)

        LOGGER.info(f"Registering Jupyter kernel '{kernel_name}' with Python: {self._python_executable}")

        # Run ipykernel install command
        result = subprocess.run(
            [
                str(self._python_executable),
                "-m",
                "ipykernel",
                "install",
                "--user",
                "--name",
                kernel_name,
                "--display-name",
                f"Python ({self.server_config.name})",
            ],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            LOGGER.error(f"Failed to register kernel: {result.stderr}")
            raise RuntimeError(f"Kernel registration failed: {result.stderr}")

        LOGGER.info(f"Kernel '{kernel_name}' registered successfully")
        LOGGER.debug(f"Kernel install output: {result.stdout}")

    async def _inject_tool_proxies(self, session_id: str) -> None:
        """
        Inject instrumented tool proxy functions into the kernel for a session.

        This is idempotent — it checks whether proxies have already been injected
        for the given session and skips if so.
        """
        if session_id in self._tool_proxies_injected:
            return

        if not self.tool_registry or not self.tool_registry.tools:
            self._tool_proxies_injected.add(session_id)
            return

        LOGGER.info(f"Injecting tool proxies into session {session_id}")

        # 1. Inject tracing infrastructure (ToolCallLog class)
        infra_code = generate_tracing_infrastructure_code()
        infra_result = await self._execute_code(infra_code, timeout=30)
        if not infra_result.success:
            LOGGER.error(f"Failed to inject tracing infrastructure: {infra_result.error}")
            raise RuntimeError(f"Tool proxy injection failed (infrastructure): {infra_result.error}")

        # 2. Inject tool proxy functions
        proxy_code = generate_tool_proxies(self.tool_registry)
        proxy_result = await self._execute_code(proxy_code, timeout=30)
        if not proxy_result.success:
            LOGGER.error(f"Failed to inject tool proxies: {proxy_result.error}")
            raise RuntimeError(f"Tool proxy injection failed (proxies): {proxy_result.error}")

        # 3. Inject list_tools() helper
        list_tools_code = generate_list_tools_code(self.tool_registry)
        list_result = await self._execute_code(list_tools_code, timeout=30)
        if not list_result.success:
            LOGGER.error(f"Failed to inject list_tools: {list_result.error}")
            raise RuntimeError(f"Tool proxy injection failed (list_tools): {list_result.error}")

        self._tool_proxies_injected.add(session_id)
        LOGGER.info(f"Successfully injected {len(self.tool_registry.tools)} tool proxies into session {session_id}")

    async def _execute_code(self, code: str, timeout: int) -> CodeExecutionResult:
        """
        Execute code in the environment's Python interpreter.

        Args:
            code: Python code to execute
            timeout: Execution timeout in seconds

        Returns:
            CodeExecutionResult with stdout, stderr, return value, etc.
        """
        # Validate code
        is_valid, error_msg = self.validate_code(code)
        if not is_valid:
            return CodeExecutionResult(success=False, error=error_msg)

        # Preprocess code
        code = self.preprocess_code(code)

        # Get session from ContextVar for kernel execution.
        session = get_current_session()
        session_id = session.session_id
        LOGGER.debug(f"Using session {session_id} for kernel execution")

        # Execute in Jupyter kernel
        try:
            start_time = time.monotonic()
            working_dir_str = str(self.working_dir) if self.working_dir else None
            stdout, stderr, success, displays, artifacts = await self.session_manager.execute_code_for_session(
                session_id=session_id, code=code, timeout=timeout, working_dir=working_dir_str
            )
            execution_time = time.monotonic() - start_time

            result = CodeExecutionResult(
                stdout=stdout,
                stderr=stderr,
                execution_time=execution_time,
                success=success,
                error=None if success else "Kernel execution failed",
                displays=displays,
                artifacts=artifacts,
            )

            result = self.postprocess_result(result)
            return result

        except Exception as e:
            LOGGER.error(f"Kernel execution error: {e}", exc_info=True)
            return CodeExecutionResult(success=False, error=f"Kernel execution failed: {e}", execution_time=0.0)

    async def _execute_code_background(self, code: str, timeout: int) -> dict[str, Any]:
        """
        Submit code for background execution in the session's current kernel.

        Returns:
            Dict with job_id/status/session_id
        """
        is_valid, error_msg = self.validate_code(code)
        if not is_valid:
            raise ValueError(error_msg)

        code = self.preprocess_code(code)
        session = get_current_session()
        session_id = session.session_id
        working_dir_str = str(self.working_dir) if self.working_dir else None
        result = await self.session_manager.start_background_execution_for_session(
            session_id=session_id, code=code, timeout=timeout, working_dir=working_dir_str
        )
        return result

    async def _execute_code_with_promotion(
        self, code: str, timeout: int, promotion_threshold_s: float
    ) -> "CodeExecutionResult | dict[str, Any]":
        """Execute code synchronously, promoting to a background job if it exceeds the threshold.

        Returns:
            CodeExecutionResult when execution completes within the threshold.
            dict (job handle with ``promoted=True``) when promoted to background.
        """
        # Validate and preprocess, matching the sync/background paths
        is_valid, error_msg = self.validate_code(code)
        if not is_valid:
            return CodeExecutionResult(success=False, error=error_msg)

        code = self.preprocess_code(code)

        session = get_current_session()
        session_id = session.session_id
        working_dir_str = str(self.working_dir) if self.working_dir else None

        start_time = time.monotonic()
        result = await self.session_manager.start_promoted_execution_for_session(
            session_id=session_id,
            code=code,
            timeout=timeout,
            promotion_threshold_s=promotion_threshold_s,
            working_dir=working_dir_str,
        )

        if isinstance(result, dict):
            # Promoted to background — return the job handle as-is
            return result

        # Completed within threshold — convert the tuple to CodeExecutionResult
        execution_time = time.monotonic() - start_time
        stdout, stderr, success, displays, artifacts = result
        execution_result = CodeExecutionResult(
            stdout=stdout,
            stderr=stderr,
            execution_time=execution_time,
            success=success,
            error=None if success else "Kernel execution failed",
            displays=displays,
            artifacts=artifacts,
        )
        execution_result = self.postprocess_result(execution_result)

        # Flush tool-call trace records, matching the sync path
        if (
            self.tool_registry
            and self.tool_registry.tools
            and session
            and session.session_id in self._tool_proxies_injected
        ):
            try:
                trace_result = await self._execute_code(FLUSH_SNIPPET, timeout=10)
                if trace_result.success and trace_result.stdout:
                    raw_calls = json.loads(trace_result.stdout.strip())
                    execution_result.tool_calls = [ToolCallRecord(**record) for record in raw_calls]
            except Exception as e:
                LOGGER.warning(f"Failed to extract tool call trace: {e}")

        return execution_result

    async def _execute_code_with_tracing(self, code: str, timeout: int) -> CodeExecutionResult:
        """
        Execute code and extract tool-call trace records from the kernel.

        Runs the agent's code, then flushes the kernel-side ToolCallLog
        to harvest structured tool-call records. The records are attached
        to the CodeExecutionResult.tool_calls field.

        Args:
            code: Python code to execute
            timeout: Execution timeout in seconds

        Returns:
            CodeExecutionResult with tool_calls populated from kernel trace log
        """
        # Run the agent's code
        result = await self._execute_code(code, timeout)

        # Only attempt trace extraction if the tool registry exists
        # and proxies were injected (otherwise no tools to trace)
        session = get_current_session()
        if (
            self.tool_registry
            and self.tool_registry.tools
            and session
            and session.session_id in self._tool_proxies_injected
        ):
            try:
                trace_result = await self._execute_code(FLUSH_SNIPPET, timeout=10)
                if trace_result.success and trace_result.stdout:
                    raw_calls = json.loads(trace_result.stdout.strip())
                    result.tool_calls = [ToolCallRecord(**record) for record in raw_calls]
                    if result.tool_calls:
                        LOGGER.info(f"Extracted {len(result.tool_calls)} tool call(s) from execution")
            except Exception as e:
                LOGGER.warning(f"Failed to extract tool call trace: {e}")
                # Non-fatal — the code execution itself succeeded/failed independently

        return self._truncate_output_if_needed(result)

    def _build_tool_catalog_summary(self) -> str:
        """Build a summary of available tools for the execute_code description."""
        if not self.tool_registry or not self.tool_registry.tools:
            return ""

        lines = ["Available tool functions (call directly in your code):"]
        for td in self.tool_registry.tools:
            sig_parts = []
            for p in td.required_parameters:
                sig_parts.append(f"{p.name}: {p.type.__name__}")
            for p in td.optional_parameters:
                sig_parts.append(f"{p.name}: {p.type.__name__} = {p.default!r}")
            sig = ", ".join(sig_parts)
            lines.append(f"  - {td.name}({sig}): {td.description}")
        lines.append("")
        lines.append("Call list_tools() in your code for full signatures and documentation.")
        return "\n".join(lines)

    def _build_tool_infos(self) -> "list[Any]":
        """Convert the server's :class:`~tool_registry.ToolRegistry` entries to
        :class:`~code_execution.tools.tool_search.ToolInfo` objects suitable for indexing.

        Returns an empty list when no tool registry is configured.
        """
        from agora_workbench.code_execution.tools.tool_search import ToolInfo

        if not self.tool_registry:
            return []

        server_name = self.server_config.name

        # Load state→affordance phrases from the states passed to the server.
        state_aff_lookup: dict[str, list[str]] = dict(self._state_affordances)

        infos: list[ToolInfo] = []
        for td in self.tool_registry.tools:
            # Merge state-derived affordances (from produced states) with tool-specific ones.
            aff: list[str] = []
            seen_aff: set[str] = set()
            for state_token in sorted(td.state_transition.produces):
                for phrase in state_aff_lookup.get(state_token, []):
                    key = phrase.strip().lower()
                    if key not in seen_aff:
                        seen_aff.add(key)
                        aff.append(phrase)
            for phrase in td.affordances:
                key = phrase.strip().lower()
                if key not in seen_aff:
                    seen_aff.add(key)
                    aff.append(phrase)

            infos.append(
                ToolInfo(
                    name=td.name,
                    description=td.description,
                    server_name=server_name,
                    affordances=tuple(aff),
                    state_requires=tuple(sorted(td.state_transition.requires)),
                    state_produces=tuple(sorted(td.state_transition.produces)),
                )
            )
        return infos

    def _setup_search_tool(self) -> None:
        """Register ``search_{name}_tools`` as an MCP tool on this server.

        Builds a search index at startup over the server's own tool catalog
        (from :attr:`tool_registry`) and any discoverable skills.  The index
        is shared across all sessions and rebuilt on each server restart.

        The registered tool is named ``search_{server_name}_tools`` so that
        agents can distinguish catalogs when connected to multiple servers.
        """
        from agora_workbench.code_execution.tools.tool_search import ToolSearchResult
        from agora_workbench.code_execution.tools import create_tool_search_backend

        server_name = self.server_config.name
        tool_name = f"search_{server_name}_tools"

        tool_infos = self._build_tool_infos()

        # Use skills passed to the server constructor.
        skills_dicts = [
            {"name": s.name, "description": s.description, "domain": s.domain, "states": s.states} for s in self.skills
        ]

        if not tool_infos and not skills_dicts:
            LOGGER.debug(
                "No tools or skills to index for '%s'; skipping search_%s_tools registration.",
                server_name,
                server_name,
            )
            return

        if self._custom_tool_search_backend is not None:
            backend = self._custom_tool_search_backend
        else:
            backend = create_tool_search_backend(
                backend_type=self.server_config.tool_search_backend,
            )
        backend.index(tools=tool_infos, skills=skills_dicts, server_name=server_name)
        self._tool_search_backends.append(backend)

        LOGGER.info(
            "Server-side tool search index built for '%s' with %d tools and %d skills",
            server_name,
            len(tool_infos),
            len(skills_dicts),
        )

        async def search_server_tools(
            query: str, top: int = 5, category: str = "all", ctx: Optional[Context] = None
        ) -> str:
            """Search this server's tool and skill catalog by name or description.

            Use this tool to discover domain tools and skills. Results are
            grouped by type. Skills must be loaded via ``load_{name}_skill``
            before use; tools can be called directly.

            Args:
                query: Natural-language description or tool/skill name to search for.
                    Pass an empty string with ``top=999`` to retrieve the full
                    catalog.
                top: Maximum number of results to return per category (default 5).
                category: Filter results — ``"all"`` (default), ``"tools"``, or
                    ``"skills"``.

            Returns:
                JSON object with ``tools`` and ``skills`` arrays. Each result
                contains ``name``, ``server_name``, ``description``, ``type``,
                ``to_access``, ``score``, ``state_requires``, and ``state_produces``.
            """
            LOGGER.info(
                "search_%s_tools called with query=%r top=%d category=%r",
                server_name,
                query,
                top,
                category,
            )
            session_id = None
            if ctx:
                try:
                    session_id = ctx.session_id
                except (RuntimeError, AttributeError):
                    # Session context may be unavailable in some execution paths;
                    # keep session_id as None and continue with the search request.
                    LOGGER.debug("Unable to resolve session_id from context", exc_info=True)
            # Validate category
            if category not in ("all", "tools", "skills"):
                return json.dumps({"error": f"Invalid category '{category}'. Must be 'all', 'tools', or 'skills'."})
            try:
                results: list[ToolSearchResult] = await backend.search(query, top, category=category)
                # Group results by type
                tools_list = [r.model_dump() for r in results if r.type == "tool"]
                skills_list = [r.model_dump() for r in results if r.type == "skill"]
                query_label = repr(query) if query else "'' (catalog)"
                self.activity_publisher.publish_nowait(
                    {
                        "type": "tool_search",
                        "description": (
                            f"search {query_label} → {len(tools_list)} tool(s), {len(skills_list)} skill(s)"
                        ),
                        "query": query,
                        "category": category,
                        "matched_tools": [t.get("name", "") for t in tools_list],
                        "matched_skills": [s.get("name", "") for s in skills_list],
                        "session_id": session_id,
                        "success": True,
                    }
                )
                payload: dict[str, Any] = {"tools": tools_list, "skills": skills_list}
                if not tools_list and not skills_list:
                    payload["hint"] = agent_guidance.no_results_hint("tools", query)
                return json.dumps(payload)
            except Exception as exc:
                LOGGER.error(
                    "search_%s_tools failed for query %r: %s",
                    server_name,
                    query,
                    exc,
                    exc_info=True,
                )
                self.activity_publisher.publish_nowait(
                    {
                        "type": "tool_search",
                        "description": f"search '{query}' failed: {type(exc).__name__}",
                        "query": query,
                        "category": category,
                        "session_id": session_id,
                        "success": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                return json.dumps({"tools": [], "skills": [], "error": f"{type(exc).__name__}: {exc}"})

        self.mcp.tool(
            name=tool_name,
            description=(
                f"Search {server_name} domain tools and skills by name or description. "
                f"Returns matching results grouped into 'tools' and 'skills' arrays. "
                f"Skills contain step-by-step workflow instructions — load them with "
                f"load_{server_name}_skill. Use category='skills' to find only skills, "
                f"or category='tools' for only tools."
            ),
        )(search_server_tools)

    def _setup_workflow_planning_tools(self) -> None:
        """Register ``plan_{name}_workflow`` and ``load_{name}_skill`` as MCP tools.

        ``plan_{name}_workflow`` is only registered when state-annotated tools
        exist (tools with ``state_requires`` or ``state_produces``).

        ``load_{name}_skill`` is registered whenever discoverable skills exist,
        regardless of whether the state graph is available.  This ensures
        skills found via ``search_{name}_tools`` can always be loaded.
        """
        from agora_workbench.code_execution.tools import (
            create_plan_workflow_descriptor,
        )

        server_name = self.server_config.name
        tool_infos = self._build_tool_infos()

        has_state_tools = any(t.state_requires or t.state_produces for t in tool_infos)

        # Register plan_{name}_workflow only when state-annotated tools exist.
        if has_state_tools:
            # Convert Skill objects to the dict format StateGraph expects
            skills_dicts = [
                {
                    "name": s.name,
                    "description": s.description,
                    "domain": s.domain,
                    "states": s.states,
                    "abs_path": s.path,
                }
                for s in self.skills
            ]
            pw_descriptor = create_plan_workflow_descriptor(
                server_name=server_name,
                tools=tool_infos,
                domain_name=server_name,
                skills=skills_dicts,
                state_descriptions={s.token: s.description for s in self.states if s.description},
            )
            _pw_func = pw_descriptor.func

            async def _plan_workflow(
                domain: str = "",
                mode: str = "overview",
                current_state: str = "",
                target_state: str = "",
                tool_name: str = "",
                ctx: Optional[Context] = None,
            ) -> str:
                """Plan and navigate domain workflow states."""
                session_id = None
                if ctx:
                    try:
                        session_id = ctx.session_id
                    except (RuntimeError, AttributeError):
                        # Some contexts may not expose session_id; leave session_id as None.
                        session_id = None
                result = await _pw_func(
                    domain=domain,
                    mode=mode,
                    current_state=current_state,
                    target_state=target_state,
                    tool_name=tool_name,
                )
                desc_bits = [f"mode={mode}"]
                if domain:
                    desc_bits.append(f"domain={domain}")
                if current_state or target_state:
                    desc_bits.append(f"{current_state or '?'}→{target_state or '?'}")
                if tool_name:
                    desc_bits.append(f"tool={tool_name}")
                self.activity_publisher.publish_nowait(
                    {
                        "type": "workflow_planned",
                        "description": "plan_workflow " + " ".join(desc_bits),
                        "domain": domain or None,
                        "mode": mode,
                        "current_state": current_state or None,
                        "target_state": target_state or None,
                        "tool_name": tool_name or None,
                        "session_id": session_id,
                        "success": True,
                    }
                )
                return result

            self.mcp.tool(
                name=pw_descriptor.name,
                description=pw_descriptor.description,
            )(_plan_workflow)

            LOGGER.info(
                "Registered plan_%s_workflow (%d state-annotated tools)",
                server_name,
                len([t for t in tool_infos if t.state_requires or t.state_produces]),
            )

        # Register load_{name}_skill whenever skills are available.
        if self.skills:
            # Build a name→content index from the Skill objects
            _skill_index = {s.name: s.content for s in self.skills}

            async def _load_skill(skill_name: str, ctx: Optional[Context] = None) -> str:
                """Load a skill by name."""
                session_id = None
                if ctx:
                    try:
                        session_id = ctx.session_id
                    except (RuntimeError, AttributeError):
                        pass

                content = _skill_index.get(skill_name)
                if content is None:
                    available = sorted(_skill_index.keys())
                    result = json.dumps({"error": f"Skill '{skill_name}' not found.", "available_skills": available})
                else:
                    result = content

                self.activity_publisher.publish_nowait(
                    {
                        "type": "skill_loaded",
                        "description": f"load_skill {skill_name}",
                        "skill_name": skill_name,
                        "session_id": session_id,
                        "success": content is not None,
                    }
                )
                return result

            ls_tool_name = f"load_{server_name}_skill"
            ls_description = (
                f"Load a {server_name} skill by name. Returns the full skill "
                f"markdown so you can follow its instructions. Use "
                f"plan_{server_name}_workflow or search_{server_name}_tools to "
                f"discover available skill names."
            )
            self.mcp.tool(name=ls_tool_name, description=ls_description)(_load_skill)

            LOGGER.info("Registered load_%s_skill (%d skills available)", server_name, len(self.skills))
        elif not has_state_tools:
            LOGGER.debug(
                "No state-annotated tools or skills found for '%s'; skipping workflow planning registration.",
                server_name,
            )

    def _load_peer_registry(self) -> "dict[str, str]":
        """Resolve the peer registry (name → base URL) for dynamic send destinations.

        Merges ``ServerConfig.peer_registry`` with the ``AGORA_PEER_REGISTRY`` env
        var (inline JSON or a path to a JSON file; env takes precedence). The
        server's own name is dropped — a server never sends to itself. Only names
        present here are reachable, so the operator keeps a single allow-list and
        the agent cannot push to arbitrary URLs.
        """
        registry: dict[str, str] = {str(k): str(v) for k, v in (self.server_config.peer_registry or {}).items()}

        raw = os.getenv("AGORA_PEER_REGISTRY", "").strip()
        if raw:
            try:
                if raw.startswith("{"):
                    env_map = json.loads(raw)
                else:
                    with open(raw, encoding="utf-8") as f:
                        env_map = json.load(f)
                if isinstance(env_map, dict):
                    registry.update({str(k): str(v) for k, v in env_map.items()})
                else:
                    LOGGER.warning("AGORA_PEER_REGISTRY is not a JSON object; ignoring")
            except (OSError, ValueError) as exc:
                LOGGER.warning("Failed to load AGORA_PEER_REGISTRY (%r): %s", raw, exc)

        registry.pop(self.server_config.name, None)
        return registry

    def _setup_send_tool(self) -> None:
        """Register the unified ``{name}_send`` MCP tool.

        This tool replaces both ``{name}_push_object`` (server-to-server) and
        ``{name}_publish_artifact`` (blob/gui/local export) with a single
        agent-facing tool that routes via ``destination_name`` on registered
        publishers.

        Architecture:
            1. Destination Router — matches ``to`` param to a publisher
            2. Materializer — resolves ``data_ref`` from kernel variable or output file
            3. Calls ``publisher.publish(local_path, name, session_id)``
        """
        from .data_access.publishers import GuiPublisher as _GuiPub, ServerPublisher as _ServerPub

        server = self
        tool_name = f"{self.server_config.name}_send"

        # Build destination enum from registered publishers and validate uniqueness.
        destination_names: list[str] = []
        for p in self._publishers:
            dn = p.destination_name
            if dn in destination_names:
                raise ValueError(
                    f"Duplicate publisher destination_name '{dn}' registered on server "
                    f"'{self.server_config.name}'. Each publisher must have a unique "
                    f"destination_name. Conflicting publisher: {type(p).__name__}."
                )
            destination_names.append(dn)

        # Peers resolvable dynamically from the shared registry — destinations the
        # agent can use even though no ServerPublisher is pre-registered for them.
        registry_peer_names = [n for n in self._peer_registry if n not in destination_names]
        all_destination_names = destination_names + registry_peer_names

        async def send(
            ctx: Context,
            data_ref: str,
            to: str,
            name: str = "",
            path: str = "",
            session_id: str = "",
        ) -> str:
            """Send data from this server to a destination (peer server, blob, user, or local).

            The ``data_ref`` is resolved in order:
            1. If it matches a kernel variable name → serialize via dill to a temp file
            2. If it matches a file in ``AGORA_OUTPUT_DIR`` → use that file directly

            The ``to`` parameter selects the destination publisher by its logical name.

            Args:
                data_ref: Kernel variable name or filename in AGORA_OUTPUT_DIR.
                to: Logical destination name — a registered publisher or a peer in the server's registry.
                name: Optional rename at destination. Defaults to ``data_ref``.
                path: Optional destination path (for blob/local publishers that need paths).
                session_id: Target session ID (for server destinations; empty = auto-resolve).

            Returns:
                JSON result with success status and transfer details.
            """
            import json as _json
            import keyword
            import os
            import tempfile
            import uuid as _uuid

            from .object_transfer import MAX_TRANSFER_SIZE_BYTES

            transfer_id = _uuid.uuid4().hex

            # --- Destination Router ---
            # 1. Prefer a pre-registered publisher (user/blob/local/static peer).
            publisher = None
            for p in server._publishers:
                if p.destination_name == to:
                    publisher = p
                    break

            # 2. Fall back to the shared peer registry: construct a ServerPublisher
            #    on demand. Only registry names (operator allow-list) are reachable;
            #    the ServerPublisher still validates the URL before sending creds.
            #    trust_http=True: the registry URL's scheme was chosen by the
            #    operator, so a plain-HTTP peer need not also be listed in
            #    OBJECT_TRANSFER_TRUSTED_HTTP_HOSTS.
            if publisher is None:
                peer_url = server._peer_registry.get(to)
                if peer_url:
                    publisher = _ServerPub(server_name=to, target_url=peer_url, trust_http=True)

            if publisher is None:
                return _json.dumps(
                    {
                        "success": False,
                        "error": (f"Unknown destination '{to}'. Available destinations: {all_destination_names}"),
                    },
                    indent=2,
                )

            # --- Session resolution ---
            mcp_session_id = None
            if ctx:
                try:
                    mcp_session_id = ctx.session_id
                except (RuntimeError, AttributeError):
                    pass

            try:
                server._restore_auth_context_for_mcp_session(mcp_session_id)
                session = await server._get_or_create_session(tool_name, session_id=mcp_session_id)
            except HTTPException as e:
                if self._is_max_sessions_http_error(e):
                    return _json.dumps(e.detail, indent=2)
                return _json.dumps({"success": False, "error": str(e)}, indent=2)
            except Exception as e:
                return _json.dumps({"success": False, "error": f"Session error: {e}"}, indent=2)

            effective_name = name or data_ref
            # For path-based destinations (blob/local), use path if provided
            logical_name = path or effective_name

            # --- Materializer ---
            # Determine if data_ref is a kernel variable or an output file.
            # Strategy: check if it's a valid identifier (potential variable) first,
            # then try to resolve from kernel; fall back to output dir file.
            is_variable = data_ref.isidentifier() and not keyword.iskeyword(data_ref)
            materialized_path = None
            temp_path = None
            is_server_destination = isinstance(publisher, _ServerPub)

            # Server destinations only accept kernel variables (serialized pickles).
            # Sending raw output-dir files to a server would fail on deserialization.
            if is_server_destination:
                if not is_variable:
                    return _json.dumps(
                        {
                            "success": False,
                            "error": (
                                f"Server destination '{to}' requires a valid Python variable name "
                                f"as data_ref, but got '{data_ref}'. Use a kernel variable name."
                            ),
                        },
                        indent=2,
                    )
                # Validate that the effective target name is a valid Python identifier
                target_var_name = name or data_ref
                if not target_var_name.isidentifier() or keyword.iskeyword(target_var_name):
                    return _json.dumps(
                        {
                            "success": False,
                            "error": f"Invalid target variable name: '{target_var_name}'. Must be a valid Python identifier.",
                        },
                        indent=2,
                    )
                # path is not meaningful for server destinations
                logical_name = target_var_name

            try:
                if is_variable:
                    # Try to serialize from kernel namespace
                    fd, temp_path = tempfile.mkstemp(prefix="_mcp_send_", suffix=".pkl")
                    os.close(fd)

                    serialize_code = (
                        f"import dill as __pkl__\n"
                        f"with open({temp_path!r}, 'wb') as __f__:\n"
                        f"    __pkl__.dump({data_ref}, __f__)\n"
                        f"del __pkl__, __f__\n"
                    )
                    working_dir_str = str(server.working_dir) if server.working_dir else None
                    (
                        stdout,
                        stderr,
                        success,
                        _displays,
                        _artifacts,
                    ) = await server.session_manager.execute_code_for_session(
                        session_id=session.session_id,
                        code=serialize_code,
                        timeout=60,
                        working_dir=working_dir_str,
                    )

                    if success and os.path.exists(temp_path) and os.path.getsize(temp_path) > 0:
                        materialized_path = Path(temp_path)
                    else:
                        # Distinguish "variable not found" (NameError) from
                        # serialization failures (dill errors, __getstate__ issues).
                        # Only fall through to file lookup on NameError.
                        error_text = stderr.strip() if stderr else ""
                        is_name_error = "NameError" in error_text

                        try:
                            os.unlink(temp_path)
                        except OSError:
                            LOGGER.debug("Failed to remove temp file %s during cleanup", temp_path, exc_info=True)
                        temp_path = None

                        if not is_name_error and error_text:
                            # Real serialization failure — report to agent
                            return _json.dumps(
                                {
                                    "success": False,
                                    "error": (f"Failed to serialize variable '{data_ref}' from kernel: {error_text}"),
                                },
                                indent=2,
                            )

                # If not materialized from kernel, try output directory
                if materialized_path is None:
                    record = server.session_manager.find_artifact_by_name(session.session_id, data_ref)
                    if record is not None:
                        materialized_path = record.path
                    else:
                        # Neither variable nor file found
                        error_detail = f"Cannot resolve '{data_ref}': "
                        if is_variable:
                            error_detail += (
                                "not found as a kernel variable or as a file in AGORA_OUTPUT_DIR. "
                                "Ensure the variable exists in the kernel namespace or the file "
                                "was written to AGORA_OUTPUT_DIR during a previous execute call."
                            )
                        else:
                            error_detail += (
                                "not a valid Python identifier and not found in AGORA_OUTPUT_DIR. "
                                "Provide a kernel variable name or a filename written to AGORA_OUTPUT_DIR."
                            )
                        return _json.dumps({"success": False, "error": error_detail}, indent=2)

                # --- Size check for server destinations ---
                if is_server_destination:
                    file_size = materialized_path.stat().st_size
                    if file_size > MAX_TRANSFER_SIZE_BYTES:
                        return _json.dumps(
                            {
                                "success": False,
                                "error": (
                                    f"Serialized object size ({file_size:,} bytes) exceeds "
                                    f"limit ({MAX_TRANSFER_SIZE_BYTES:,} bytes). "
                                    "Reduce the object size or split into smaller pieces."
                                ),
                            },
                            indent=2,
                        )

                # --- Publish ---
                # Create per-call copies to avoid race conditions with
                # concurrent requests mutating shared publisher state.
                import copy as _copy

                pub_instance = publisher

                if isinstance(publisher, _GuiPub):
                    # GuiPublisher needs the download token
                    record = server.session_manager.find_artifact_by_name(session.session_id, data_ref)
                    if record is not None:
                        pub_instance = _copy.copy(publisher)
                        pub_instance._download_token = record.token
                    else:
                        return _json.dumps(
                            {
                                "success": False,
                                "error": (
                                    f"Cannot send '{data_ref}' to 'user': artifact must be a file "
                                    "in AGORA_OUTPUT_DIR (not a kernel variable) for GUI download."
                                ),
                            },
                            indent=2,
                        )

                if isinstance(publisher, _ServerPub):
                    # ServerPublisher needs the bearer token and metadata
                    pub_instance = _copy.copy(publisher)
                    current_token = get_current_request_token() or session.user_token
                    pub_instance._user_token = current_token
                    pub_instance._source_server = server.server_config.name
                    pub_instance._transfer_id = transfer_id

                target_session = session_id if is_server_destination else session.session_id
                remote_uri = await pub_instance.publish(
                    local_path=materialized_path,
                    name=logical_name,
                    session_id=target_session,
                )

                # --- Activity event ---
                # Activity event types: server destinations → object_sent,
                # file destinations → artifact_published.
                event_type = "object_sent" if is_server_destination else "artifact_published"
                server.activity_publisher.publish_nowait(
                    {
                        "type": event_type,
                        "description": f"send '{data_ref}' → {to}",
                        "transfer_id": transfer_id,
                        "data_ref": data_ref,
                        "destination": to,
                        "name": logical_name,
                        "session_id": session.session_id,
                        "success": True,
                        "remote_uri": remote_uri,
                    }
                )

                return _json.dumps(
                    {
                        "success": True,
                        "data_ref": data_ref,
                        "destination": to,
                        "name": logical_name,
                        "remote_uri": remote_uri,
                        "transfer_id": transfer_id,
                    },
                    indent=2,
                )

            except Exception as exc:
                LOGGER.error("send tool failed: %s", exc, exc_info=True)
                event_type = "object_sent" if is_server_destination else "artifact_published"
                server.activity_publisher.publish_nowait(
                    {
                        "type": event_type,
                        "description": f"send '{data_ref}' → {to} failed: {type(exc).__name__}",
                        "transfer_id": transfer_id,
                        "data_ref": data_ref,
                        "destination": to,
                        "session_id": session.session_id if session else mcp_session_id,
                        "success": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                return _json.dumps(
                    {"success": False, "error": f"{type(exc).__name__}: {exc}"},
                    indent=2,
                )
            finally:
                server._clear_auth_context()
                # Clean up temp file if we created one
                if temp_path:
                    try:
                        os.unlink(temp_path)
                    except OSError:
                        pass

        # Build dynamic description with available destinations
        dest_examples = []
        for p in self._publishers:
            dn = p.destination_name
            if isinstance(p, _ServerPub):
                dest_examples.append(
                    f'  {self.server_config.name}_send(data_ref="my_var", to="{dn}") — '
                    f"transfer variable to {dn} server's kernel"
                )
            elif isinstance(p, _GuiPub):
                dest_examples.append(
                    f'  {self.server_config.name}_send(data_ref="results.csv", to="user") — '
                    "make file downloadable in browser"
                )
            elif dn == "blob":
                dest_examples.append(
                    f'  {self.server_config.name}_send(data_ref="results.csv", to="blob", '
                    f'path="reports/results.csv") — upload to blob storage'
                )
            elif dn == "local":
                dest_examples.append(
                    f'  {self.server_config.name}_send(data_ref="output.csv", to="local") — copy to local filesystem'
                )

        for peer_name in registry_peer_names:
            dest_examples.append(
                f'  {self.server_config.name}_send(data_ref="my_var", to="{peer_name}") — '
                f"transfer variable to {peer_name} server's kernel (resolved via peer registry)"
            )

        examples_text = "\n".join(dest_examples)
        dest_list = ", ".join(f"'{d}'" for d in all_destination_names)

        description = (
            f"Send data from the {self.server_config.name} server to a destination. "
            "This is the unified tool for all data transfer — to peer servers, "
            "blob storage, local filesystem, or browser download.\n\n"
            f"Available destinations (to parameter): {dest_list}\n\n"
            "The `data_ref` parameter accepts either:\n"
            "- A Python variable name from the kernel namespace (for server-to-server transfers)\n"
            "- A filename written to AGORA_OUTPUT_DIR during a previous execute call\n\n"
            "Examples:\n"
            f"{examples_text}\n\n"
            "Parameters:\n"
            "- data_ref (required): Kernel variable name or filename in AGORA_OUTPUT_DIR\n"
            f"- to (required): Destination name — one of: {dest_list}\n"
            "- name (optional): Rename at destination (defaults to data_ref)\n"
            "- path (optional): Full destination path (for blob/local destinations)\n"
            "- session_id (optional): Target session ID (for server destinations; auto-resolved if empty)"
        )

        self.mcp.tool(name=tool_name, description=description)(send)
        LOGGER.info(
            "Registered unified send tool: %s (static: %s, registry peers: %s)",
            tool_name,
            destination_names,
            registry_peer_names,
        )

    async def _inspect_session_payload(self, session_id: str) -> dict[str, Any]:
        """Inspect a session's namespace and background job status."""
        self.session_manager.get_session(session_id)
        inspect_code = """
import json as __agora_json__

def __agora_safe_repr__(value, limit=300):
    try:
        text = repr(value)
    except Exception as exc:  # pragma: no cover - defensive
        text = f"<repr-error {type(exc).__name__}: {exc}>"
    if len(text) > limit:
        text = text[:limit] + "...<truncated>"
    return text

__agora_ns__ = {}
for __name__, __value__ in list(globals().items()):
    if __name__.startswith("_"):
        continue
    __agora_ns__[__name__] = {
        "type": type(__value__).__name__,
        "repr": __agora_safe_repr__(__value__),
    }

print(__agora_json__.dumps(__agora_ns__, sort_keys=True))
"""
        stdout, _stderr, success, _displays, _artifacts = await self.session_manager.execute_code_for_session(
            session_id=session_id,
            code=inspect_code,
            timeout=10,
            working_dir=str(self.working_dir) if self.working_dir else None,
        )

        namespace: dict[str, Any] = {}
        if success and stdout.strip():
            namespace = json.loads(stdout.strip().splitlines()[-1])

        async with self._parallel_state_lock:
            latest_job_id = self._parallel_job_by_session.get(session_id)
            latest_job = self._parallel_jobs.get(latest_job_id or "")
            running_job = next(
                (
                    job
                    for job in self._parallel_jobs.values()
                    if job["session_id"] == session_id and job["status"] == "running"
                ),
                None,
            )

        job = running_job or latest_job
        return {
            "session_id": session_id,
            "status": "busy" if running_job else "idle",
            "job_id": job["job_id"] if job else None,
            "job_status": job["status"] if job else None,
            "namespace": namespace,
        }

    def _build_parallel_code(self, code: str, input_values: dict[str, Any]) -> str:
        """Inject input values into a code template."""
        lines: list[str] = []
        for key, value in input_values.items():
            if not isinstance(key, str) or not key.isidentifier() or keyword.iskeyword(key):
                raise ValueError(
                    f"Invalid input key '{key}'. Keys must be valid Python identifiers and not reserved keywords."
                )
            lines.append(f"{key} = {value!r}")

        return f"{chr(10).join(lines)}\n\n{code}" if lines else code

    async def _capture_result_variable(self, session_id: str, result_variable: str) -> Any:
        """Capture a result variable from a completed session."""
        if not result_variable:
            return None

        capture_code = f"""
import json as __agora_json__
if {result_variable!r} in globals():
    try:
        print("__AGORA_JSON__" + __agora_json__.dumps({result_variable}))
    except Exception:
        print("__AGORA_REPR__" + repr({result_variable}))
else:
    print("__AGORA_MISSING__")
"""
        stdout, _stderr, success, _displays, _artifacts = await self.session_manager.execute_code_for_session(
            session_id=session_id,
            code=capture_code,
            timeout=10,
            working_dir=str(self.working_dir) if self.working_dir else None,
        )
        if not success:
            return None

        for line in reversed(stdout.splitlines()):
            if line.startswith("__AGORA_JSON__"):
                payload = line[len("__AGORA_JSON__") :]
                try:
                    return json.loads(payload)
                except json.JSONDecodeError:
                    return payload
            if line.startswith("__AGORA_REPR__"):
                return line[len("__AGORA_REPR__") :]
            if line.startswith("__AGORA_MISSING__"):
                return None
        return None

    async def _run_parallel_job(
        self,
        *,
        job_id: str,
        session_id: str,
        code: str,
        timeout: int,
        result_variable: str,
        batch_id: str,
        semaphore: Optional[asyncio.Semaphore] = None,
    ) -> None:
        """Execute a parallel job in a dedicated session.

        If *semaphore* is provided the job waits to acquire it before starting
        execution, implementing server-wide concurrency limiting.  The semaphore
        is only released if it was successfully acquired, so a CancelledError
        raised while waiting does not release a slot that was never taken.
        """
        try:
            if semaphore is not None:
                # CancelledError raised here means the task was cancelled while
                # waiting for a free slot.  We haven't acquired anything, so the
                # except block below handles the status update and we propagate.
                await semaphore.acquire()
            try:
                session = self.session_manager.get_session(session_id)
                set_current_session(session)
                await self._inject_tool_proxies(session_id)
                result = await self._execute_code_with_tracing(code=code, timeout=timeout)
                self.session_manager.update_session(session_id, session)

                payload = await self._capture_result_variable(session_id, result_variable) if result.success else None
                status = "completed" if result.success else "failed"
                async with self._parallel_state_lock:
                    self._parallel_jobs[job_id].update(
                        {
                            "status": status,
                            "completed_at": time.monotonic(),
                            "result": result.model_dump(),
                            "result_payload": payload,
                        }
                    )
                self.activity_publisher.publish_nowait(
                    {
                        "type": "code_executed" if result.success else "code_failed",
                        "description": f"Parallel job {job_id} {status}",
                        "code": code,
                        "stdout": result.stdout,
                        "stderr": result.stderr,
                        "success": result.success,
                        "duration_ms": (result.execution_time or 0.0) * 1000.0,
                        "tool_calls": [tc.model_dump() for tc in result.tool_calls],
                        "error": result.error,
                        "session_id": session_id,
                        "job_id": job_id,
                        "batch_id": batch_id,
                        "displays": result.displays,
                    }
                )
            finally:
                if semaphore is not None:
                    semaphore.release()
                set_current_session(None)
        except asyncio.CancelledError:
            async with self._parallel_state_lock:
                self._parallel_jobs[job_id].update({"status": "cancelled", "completed_at": time.monotonic()})
            self.activity_publisher.publish_nowait(
                {
                    "type": "code_failed",
                    "description": f"Parallel job {job_id} cancelled",
                    "code": code,
                    "success": False,
                    "error": "cancelled",
                    "session_id": session_id,
                    "job_id": job_id,
                    "batch_id": batch_id,
                }
            )
            raise
        except Exception as exc:
            LOGGER.error("Parallel job %s failed: %s", job_id, exc, exc_info=True)
            async with self._parallel_state_lock:
                self._parallel_jobs[job_id].update(
                    {
                        "status": "failed",
                        "completed_at": time.monotonic(),
                        "result": CodeExecutionResult(success=False, error=str(exc)).model_dump(),
                    }
                )
            self.activity_publisher.publish_nowait(
                {
                    "type": "code_failed",
                    "description": f"Parallel job {job_id} failed",
                    "code": code,
                    "success": False,
                    "error": str(exc),
                    "session_id": session_id,
                    "job_id": job_id,
                    "batch_id": batch_id,
                }
            )

    async def _cleanup_parallel_batch_sessions(self, batch_id: str) -> None:
        """Close child sessions for a completed/cancelled batch once."""
        async with self._parallel_state_lock:
            batch = self._parallel_batches.get(batch_id)
            if not batch or batch.get("cleanup_done"):
                return
            session_ids = [self._parallel_jobs[job_id]["session_id"] for job_id in batch["job_ids"]]
            batch["cleanup_done"] = True

        for session_id in session_ids:
            try:
                self.session_manager.close_session(session_id)
            except Exception:
                LOGGER.debug("Failed to close parallel child session %s", session_id, exc_info=True)

    async def _prune_parallel_batch(self, batch_id: str) -> None:
        """Remove a terminal batch and its jobs from the in-memory registries.

        Called after the final payload has been returned to the caller so that
        long-lived servers don't accumulate completed/cancelled batch state.
        """
        async with self._parallel_state_lock:
            batch = self._parallel_batches.pop(batch_id, None)
            if batch:
                for job_id in batch["job_ids"]:
                    job = self._parallel_jobs.pop(job_id, None)
                    if job:
                        self._parallel_job_by_session.pop(job["session_id"], None)

    async def _get_batch_parent_session_id(self, batch_id: str) -> Optional[str]:
        """Return the session that owns a batch, without touching batch state.

        Authorization must be settled before any payload is built, because
        building one retires a terminal batch (closing child sessions and
        pruning the registries). Looking the owner up separately keeps an
        unauthorized caller from destroying results they cannot read.
        """
        async with self._parallel_state_lock:
            batch = self._parallel_batches.get(batch_id)
            return batch["parent_session_id"] if batch else None

    async def _check_batch_payload(self, batch_id: str) -> dict[str, Any]:
        """Build aggregate status for a parallel batch."""
        async with self._parallel_state_lock:
            batch = self._parallel_batches.get(batch_id)
            if not batch:
                raise ValueError(f"Batch {batch_id} not found")
            jobs = [self._parallel_jobs[job_id].copy() for job_id in batch["job_ids"]]
            parent_session_id = batch["parent_session_id"]

        running = 0
        completed = 0
        failed = 0
        job_payloads: list[dict[str, Any]] = []
        now = time.monotonic()
        for job in jobs:
            status = job["status"]
            if status == "running":
                running += 1
            elif status == "completed":
                completed += 1
            else:
                failed += 1

            item: dict[str, Any] = {
                "job_id": job["job_id"],
                "session_id": job["session_id"],
                "status": status,
                "input_index": job["input_index"],
            }
            if status == "running":
                item["elapsed_seconds"] = now - job["started_at"]
            if status in {"completed", "failed", "cancelled"}:
                item["result_variable"] = batch["result_variable"]
                item["result"] = job.get("result_payload")
                if job.get("result"):
                    item["execution"] = job["result"]
            job_payloads.append(item)

        if running > 0:
            batch_status = "running"
        elif failed > 0:
            batch_status = "partial_failure"
        else:
            batch_status = "completed"

        payload = {
            "batch_id": batch_id,
            "parent_session_id": parent_session_id,
            "status": batch_status,
            "completed": completed,
            "running": running,
            "failed": failed,
            "jobs": job_payloads,
        }
        if running == 0:
            await self._cleanup_parallel_batch_sessions(batch_id)
            await self._prune_parallel_batch(batch_id)
        return payload

    async def _parallel_execute_for_session(
        self,
        *,
        parent_session: "Session",
        code: str,
        inputs: list[dict[str, Any]],
        timeout: int,
        result_variable: str,
    ) -> dict[str, Any]:
        """Start a parallel batch for a parent session."""
        if timeout <= 0:
            raise ValueError("Timeout must be greater than 0 seconds.")
        timeout = min(timeout, self.max_timeout)

        if not isinstance(inputs, list) or not inputs:
            raise ValueError("'inputs' must be a non-empty list of dictionaries")
        if not all(isinstance(item, dict) for item in inputs):
            raise ValueError("Every entry in 'inputs' must be a dictionary")
        if not isinstance(result_variable, str) or (
            result_variable and (not result_variable.isidentifier() or keyword.iskeyword(result_variable))
        ):
            raise ValueError("'result_variable' must be a valid Python identifier")

        # Pre-validate all inputs before creating any sessions/tasks to avoid
        # orphaned child sessions and partial batch state on validation failure.
        built_codes: list[str] = []
        for input_index, input_values in enumerate(inputs):
            try:
                built_codes.append(self._build_parallel_code(code, input_values))
            except ValueError as exc:
                raise ValueError(f"Invalid inputs[{input_index}]: {exc}") from exc

        batch_id = f"b_{uuid.uuid4().hex[:12]}"
        jobs_out: list[dict[str, Any]] = []
        async with self._parallel_state_lock:
            self._parallel_batches[batch_id] = {
                "batch_id": batch_id,
                "parent_session_id": parent_session.session_id,
                "created_at": time.monotonic(),
                "job_ids": [],
                "result_variable": result_variable,
                "cleanup_done": False,
            }

            for input_index, full_code in enumerate(built_codes):
                child_session_id = self.session_manager.create_session(
                    data={},
                    user_identity=parent_session.user_identity,
                    user_token=parent_session.user_token,
                    token_claims=parent_session.token_claims,
                    metadata={
                        "type": "parallel_child",
                        "parent_session_id": parent_session.session_id,
                        "batch_id": batch_id,
                        "input_index": input_index,
                    },
                )
                job_id = f"j_{uuid.uuid4().hex[:12]}"
                job_state = {
                    "job_id": job_id,
                    "batch_id": batch_id,
                    "session_id": child_session_id,
                    "input_index": input_index,
                    "status": "running",
                    "started_at": time.monotonic(),
                    "completed_at": None,
                    "result": None,
                    "result_payload": None,
                    "task": None,
                }
                self._parallel_jobs[job_id] = job_state
                self._parallel_job_by_session[child_session_id] = job_id
                self._parallel_batches[batch_id]["job_ids"].append(job_id)
                jobs_out.append(
                    {
                        "job_id": job_id,
                        "session_id": child_session_id,
                        "status": "running",
                        "input_index": input_index,
                    }
                )

                task = asyncio.create_task(
                    self._run_parallel_job(
                        job_id=job_id,
                        session_id=child_session_id,
                        code=full_code,
                        timeout=timeout,
                        result_variable=result_variable,
                        batch_id=batch_id,
                        semaphore=self._parallel_semaphore,
                    )
                )
                job_state["task"] = task
        return {"batch_id": batch_id, "jobs": jobs_out}

    async def _cancel_batch_payload(self, batch_id: str) -> dict[str, Any]:
        """Cancel a parallel batch."""
        async with self._parallel_state_lock:
            batch = self._parallel_batches.get(batch_id)
            if not batch:
                raise ValueError(f"Batch {batch_id} not found")
            job_ids = list(batch["job_ids"])
            tasks = [self._parallel_jobs[job_id].get("task") for job_id in job_ids]
            session_ids = [self._parallel_jobs[job_id]["session_id"] for job_id in job_ids]

        for session_id in session_ids:
            kernel = self.session_manager._kernels.get(session_id)
            if kernel:
                km, _kc = kernel
                try:
                    km.interrupt_kernel()
                except Exception:
                    LOGGER.debug("Failed to interrupt kernel for session %s", session_id, exc_info=True)

        active_tasks = [task for task in tasks if task and not task.done()]
        for task in active_tasks:
            task.cancel()

        if active_tasks:
            await asyncio.gather(*active_tasks, return_exceptions=True)
        await self._cleanup_parallel_batch_sessions(batch_id)
        payload = await self._check_batch_payload(batch_id)
        payload["status"] = "partial_failure" if payload["failed"] else payload["status"]
        return payload

    def _setup_parallel_execution_tools(self) -> None:
        """Register map-style parallel execution tools."""
        execute_name = f"{self.server_config.name}_parallel_execute"
        check_name = f"{self.server_config.name}_check_batch"
        cancel_name = f"{self.server_config.name}_cancel_batch"

        async def parallel_execute(
            ctx: Context,
            code: str,
            inputs: list[dict[str, Any]],
            timeout: int = 3600,
            result_variable: str = "result",
        ) -> str:
            transport_session_id = None
            if ctx:
                try:
                    transport_session_id = ctx.session_id
                except (RuntimeError, AttributeError):
                    pass

            try:
                self._restore_auth_context_for_mcp_session(transport_session_id)
                parent_session = await self._get_or_create_session(execute_name, session_id=transport_session_id)
                payload = await self._parallel_execute_for_session(
                    parent_session=parent_session,
                    code=code,
                    inputs=inputs,
                    timeout=timeout,
                    result_variable=result_variable,
                )
                return json.dumps(payload, indent=2)
            except HTTPException as e:
                if self._is_max_sessions_http_error(e):
                    return json.dumps(e.detail, indent=2)
                LOGGER.error(f"Parallel execution setup failed: {e}", exc_info=True)
                return json.dumps({"success": False, "error": str(e)}, indent=2)
            except Exception as e:
                LOGGER.error(f"Parallel execution setup failed: {e}", exc_info=True)
                return json.dumps({"success": False, "error": str(e)}, indent=2)
            finally:
                self._clear_auth_context()

        async def _restore_auth_and_verify_batch_access(ctx: Context, batch_id: str) -> str:
            transport_session_id = None
            if ctx:
                try:
                    transport_session_id = ctx.session_id
                except (RuntimeError, AttributeError):
                    pass

            self._restore_auth_context_for_mcp_session(transport_session_id)
            parent_session_id = await self._get_batch_parent_session_id(batch_id)
            if not parent_session_id:
                raise HTTPException(status_code=404, detail=f"Batch '{batch_id}' not found")
            parent_session = self.session_manager.get_session(parent_session_id)
            request_token = get_current_request_token()
            if not await self._verify_session_ownership(parent_session, request_token):
                raise HTTPException(status_code=403, detail="Not authorized to access this batch")
            return parent_session_id

        async def check_batch(ctx: Context, batch_id: str) -> str:
            try:
                await _restore_auth_and_verify_batch_access(ctx, batch_id)
                payload = await self._check_batch_payload(batch_id)
                return json.dumps(payload, indent=2)
            except Exception as e:
                LOGGER.error(f"check_batch failed for {batch_id}: {e}", exc_info=True)
                return json.dumps({"success": False, "error": str(e)}, indent=2)
            finally:
                self._clear_auth_context()

        async def cancel_batch(ctx: Context, batch_id: str) -> str:
            transport_session_id = None
            if ctx:
                try:
                    transport_session_id = ctx.session_id
                except (RuntimeError, AttributeError):
                    # ctx/session metadata may be unavailable for some transports; fall back to None.
                    pass
            try:
                await _restore_auth_and_verify_batch_access(ctx, batch_id)
                payload = await self._cancel_batch_payload(batch_id)
                self.activity_publisher.publish_nowait(
                    {
                        "type": "batch_cancelled",
                        "description": f"cancel_batch {batch_id} ({payload.get('status')})",
                        "batch_id": batch_id,
                        "session_id": payload.get("parent_session_id") or transport_session_id,
                        "success": True,
                    }
                )
                return json.dumps(payload, indent=2)
            except Exception as e:
                LOGGER.error(f"cancel_batch failed for {batch_id}: {e}", exc_info=True)
                self.activity_publisher.publish_nowait(
                    {
                        "type": "batch_cancelled",
                        "description": f"cancel_batch {batch_id} failed: {type(e).__name__}",
                        "batch_id": batch_id,
                        "session_id": transport_session_id,
                        "success": False,
                        "error": str(e),
                    }
                )
                return json.dumps({"success": False, "error": str(e)}, indent=2)
            finally:
                self._clear_auth_context()

        self.mcp.tool(
            name=execute_name,
            description=(
                "Execute the same code template across multiple input dictionaries in parallel. "
                "A dedicated child session/kernel is created per input and run in background."
            ),
        )(parallel_execute)
        self.mcp.tool(name=check_name, description="Check aggregate status and available results for a parallel batch")(
            check_batch
        )
        self.mcp.tool(
            name=cancel_name,
            description="Cancel all running jobs in a parallel batch and clean up child sessions",
        )(cancel_batch)

    # ========================================================================
    # Authentication
    # ========================================================================

    # ========================================================================
    # BaseMCPServer abstract implementations
    # ========================================================================

    async def _health_payload(self) -> dict[str, Any]:
        """Return health check payload."""
        return {
            "status": "healthy",
            "environment_ready": self._environment_ready,
        }

    async def _catalog_payload(self) -> dict[str, Any]:
        """Return tool catalog for connector aggregation."""
        tools_data = []
        if self.tool_registry:
            tools_data = [t.model_dump(mode="json") for t in self.tool_registry.tools]

        skills_data: list[dict[str, Any]] = [
            {
                "name": s.name,
                "description": s.description,
                "domain": s.domain,
                "states": s.states,
            }
            for s in self.skills
        ]

        return {
            "server_name": self.server_config.name,
            "tools": tools_data,
            "skills": skills_data,
        }

    # ========================================================================
    # Server Lifecycle
    # ========================================================================

    async def _initialize_tool_search_backends(self) -> None:
        """Initialize any async-capable tool search backends."""
        for backend in self._tool_search_backends:
            initialize = getattr(backend, "initialize", None)
            if callable(initialize):
                result = initialize()
                if inspect.isawaitable(result):
                    await result

    async def _close_tool_search_backends(self) -> None:
        """Close registered tool search backends."""
        for backend in self._tool_search_backends:
            close = getattr(backend, "close", None)
            if callable(close):
                result = close()
                if inspect.isawaitable(result):
                    await result

    async def _startup(self):
        """Initialize environment and register kernel on server startup."""
        LOGGER.info("Initializing server...")

        # Build environment if needed
        await self._ensure_environment()

        # Expose asset cache directory via env var so kernel-side tool
        # implementations can locate pre-provisioned assets without hardcoding
        # paths.  Set on the server process so all spawned kernels inherit it.
        cache_dir = self.server_config.get_cache_dir()
        os.environ.setdefault("MCP_ASSET_CACHE_DIR", str(cache_dir))

        # Launch declared sidecars now that the environment exists (env-Python
        # sidecars need the built kernel env). Each sidecar's base URL is
        # exported to os.environ so kernels spawned below inherit it. This must
        # precede kernel registration so the discovery env var is in place.
        await self._sidecar_manager.start_all()

        # Register the environment as a Jupyter kernel
        await self._register_kernel(kernel_name="tools-py")

        await self._initialize_tool_search_backends()

        # Start the activity publisher (no-op if ACTIVITY_UI_URL not set).
        # Wrapped defensively: observability must never block server startup.
        try:
            await self.activity_publisher.start()
        except Exception:
            LOGGER.warning("ActivityPublisher failed to start; continuing without it", exc_info=True)

        LOGGER.info("Server initialization complete")

    async def _shutdown(self):
        """Clean up resources on server shutdown."""
        LOGGER.info("Shutting down server...")
        try:
            await self._sidecar_manager.stop_all()
        except Exception:
            LOGGER.warning("Sidecar shutdown raised; continuing", exc_info=True)
        await self._close_tool_search_backends()
        for publisher in self._publishers:
            try:
                await publisher.close()
            except Exception:
                LOGGER.debug("Publisher close raised; ignoring during shutdown", exc_info=True)
        try:
            await self.activity_publisher.stop()
        except Exception:
            LOGGER.debug("ActivityPublisher stop raised; ignoring during shutdown", exc_info=True)
        LOGGER.info("Server shutdown complete")

    def _add_custom_endpoints(self, app):
        """Add custom endpoints to FastMCP."""
        # Base class adds /health, /healthz, /catalog, /.well-known/oauth-protected-resource
        super()._add_custom_endpoints(app)

        # Artifact download endpoint: streams a file produced by an execute
        # under the session's outputs dir.  The auth middleware enforces a
        # valid Bearer token; the (session_id, token) pair is the authz
        # check — only sessions the caller can otherwise reach contain the
        # token mapping, and tokens are unguessable UUIDs.  Filename in the
        # URL is purely cosmetic so browser save-as picks a sensible default.
        async def download_artifact(request: Request):
            session_id = request.path_params["session_id"]
            token = request.path_params["token"]
            record = self.session_manager.get_artifact_record(session_id, token)
            if record is None:
                return JSONResponse(
                    {"error": "artifact not found"},
                    status_code=404,
                )
            return FileResponse(
                path=str(record.path),
                media_type=record.mime_type,
                filename=Path(record.name).name,
            )

        app.routes.append(
            Route(
                "/artifacts/{session_id}/{token}/{filename:path}",
                download_artifact,
                methods=["GET"],
                name="download_artifact",
            )
        )

        # Add object transfer receive endpoint for server-to-server object transfer
        async def object_transfer_receive(request: Request):
            """Receive a serialized object from another MCP server.

            Expects a JSON body with:
                - variable_name (str): Python variable name to assign in the kernel namespace
                - data (str): Base64-encoded serialized object bytes
                - session_id (str, optional): Target session ID (uses first active
                  session owned by the caller if omitted)

            The endpoint is protected by the same auth middleware as /mcp.
            The authenticated user identity must match the target session owner.

            The received object is deserialized and injected directly into the
            target session's Jupyter kernel namespace so the agent can reference
            it by variable name in subsequent code execution calls.
            """
            import keyword
            import math
            import os
            import tempfile

            from .object_transfer import ObjectSerializer, MAX_TRANSFER_SIZE_BYTES

            try:
                body = await request.json()
            except Exception:
                return JSONResponse({"success": False, "error": "Invalid JSON body"}, status_code=400)

            variable_name = body.get("variable_name")
            data_b64 = body.get("data")
            session_id = body.get("session_id")
            transfer_metadata = body.get("metadata") or {}

            if not isinstance(variable_name, str) or not isinstance(data_b64, str):
                return JSONResponse(
                    {"success": False, "error": "'variable_name' and 'data' must be strings"},
                    status_code=400,
                )

            if not variable_name or not data_b64:
                return JSONResponse(
                    {"success": False, "error": "Missing required fields: 'variable_name' and 'data'"},
                    status_code=400,
                )

            # Validate variable_name is a safe Python identifier
            if not variable_name.isidentifier() or keyword.iskeyword(variable_name):
                return JSONResponse(
                    {"success": False, "error": f"Invalid Python variable name: '{variable_name}'"},
                    status_code=400,
                )

            # Enforce size limit on the base64 payload before decoding.
            # Base64 encodes 3 bytes as 4 chars, so ceil(n * 4/3) + padding.
            max_b64_len = math.ceil(MAX_TRANSFER_SIZE_BYTES * 4 / 3) + 4
            if len(data_b64) > max_b64_len:
                return JSONResponse(
                    {"success": False, "error": "Payload exceeds maximum transfer size"},
                    status_code=413,
                )

            try:
                serialized_data = ObjectSerializer.from_base64(data_b64)
            except Exception as e:
                return JSONResponse(
                    {"success": False, "error": f"Invalid base64 data: {e}"},
                    status_code=400,
                )

            if len(serialized_data) > MAX_TRANSFER_SIZE_BYTES:
                return JSONResponse(
                    {"success": False, "error": "Decoded payload exceeds maximum transfer size"},
                    status_code=413,
                )

            # Get the authenticated user identity from context (set by AuthMiddleware)
            caller_identity = get_current_user_identity()
            if not caller_identity:
                return JSONResponse(
                    {"success": False, "error": "Authentication required"},
                    status_code=401,
                )

            # Find target session
            session = None
            if session_id:
                try:
                    session = self.session_manager.get_session(session_id)
                except (ValueError, KeyError):
                    return JSONResponse(
                        {"success": False, "error": f"Session '{session_id}' not found"},
                        status_code=404,
                    )
            else:
                # Use the first active session owned by the caller
                sessions = self.session_manager.list_sessions()
                for s_info in sessions:
                    try:
                        candidate = self.session_manager.get_session(s_info["session_id"])
                        if candidate.user_identity == caller_identity:
                            session = candidate
                            break
                    except (ValueError, KeyError):
                        continue

            if not session:
                return JSONResponse(
                    {"success": False, "error": "No active session found to receive the object"},
                    status_code=404,
                )

            # Verify session ownership: caller must own the target session
            if session.user_identity != caller_identity:
                LOGGER.warning(
                    f"Object transfer rejected: caller {caller_identity} does not own "
                    f"session {session.session_id} (owner: {session.user_identity})"
                )
                return JSONResponse(
                    {"success": False, "error": "Not authorized to write to this session"},
                    status_code=403,
                )

            # Inject the object into the kernel namespace via temp file
            fd, temp_path = tempfile.mkstemp(prefix="_mcp_transfer_", suffix=".pkl")
            os.close(fd)
            try:
                # Write serialized bytes to temp file
                with open(temp_path, "wb") as f:
                    f.write(serialized_data)

                # Deserialize and assign in the kernel
                deserialize_code = (
                    f"import dill as __pkl__\n"
                    f"with open({temp_path!r}, 'rb') as __f__:\n"
                    f"    {variable_name} = __pkl__.load(__f__)\n"
                    f"del __pkl__, __f__\n"
                )
                working_dir_str = str(self.working_dir) if self.working_dir else None
                stdout, stderr, success, _displays, _artifacts = await self.session_manager.execute_code_for_session(
                    session_id=session.session_id, code=deserialize_code, timeout=60, working_dir=working_dir_str
                )

                if not success:
                    error_msg = stderr.strip() or "Failed to inject variable into kernel"
                    LOGGER.error(
                        "object_transfer_receive: kernel injection failed for "
                        "session=%s variable=%s source=%s transfer_id=%s: %s",
                        session.session_id,
                        variable_name,
                        transfer_metadata.get("source_server"),
                        transfer_metadata.get("transfer_id"),
                        error_msg,
                    )
                    self.activity_publisher.publish_nowait(
                        {
                            "type": "object_received",
                            "description": (
                                f"Receive '{variable_name}' from "
                                f"{transfer_metadata.get('source_server') or 'another server'} "
                                f"failed: kernel injection error"
                            ),
                            "transfer_id": transfer_metadata.get("transfer_id"),
                            "variable_name": variable_name,
                            "source_server": transfer_metadata.get("source_server"),
                            "session_id": session.session_id,
                            "success": False,
                            "error": error_msg,
                        }
                    )
                    # If the receiver kernel is missing a module needed to
                    # deserialize the object, surface that as an actionable
                    # hint so the agent can self-correct (push a portable
                    # representation: pandas DataFrame, dict, JSON, ...).
                    response_body: dict[str, Any] = {
                        "success": False,
                        "error": f"Kernel injection failed: {error_msg}",
                    }
                    if "ModuleNotFoundError" in error_msg:
                        response_body["hint"] = (
                            "The target server's Python environment does not have a "
                            "module needed to deserialize this object. Push a portable "
                            "representation instead (e.g. pandas DataFrame, dict, JSON, "
                            "or the framework's own export format) rather than the live "
                            "in-memory class instance."
                        )
                    return JSONResponse(response_body, status_code=500)

                self.activity_publisher.publish_nowait(
                    {
                        "type": "object_received",
                        "description": (
                            f"Received '{variable_name}' from "
                            f"{transfer_metadata.get('source_server') or 'another server'}"
                        ),
                        "transfer_id": transfer_metadata.get("transfer_id"),
                        "variable_name": variable_name,
                        "source_server": transfer_metadata.get("source_server"),
                        "session_id": session.session_id,
                        "success": True,
                    }
                )

                return JSONResponse(
                    {
                        "success": True,
                        "variable_name": variable_name,
                        "session_id": session.session_id,
                        "size_bytes": len(serialized_data),
                    }
                )
            except Exception as e:
                LOGGER.error(f"Failed to receive object transfer: {e}", exc_info=True)
                return JSONResponse(
                    {"success": False, "error": f"Failed to import object: {e}"},
                    status_code=500,
                )
            finally:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

        app.routes.append(Route("/object-transfer/receive", object_transfer_receive, methods=["POST"]))

    def _auth_protected_paths(self) -> list[str]:
        """Paths requiring Bearer auth — extends base with /object-transfer/."""
        return ["/mcp", "/object-transfer/"]
