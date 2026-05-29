"""
ConnectorServer — lightweight MCP server that proxies to upstream domain servers.

The ConnectorServer has no Python kernel or execution environment. It:
1. Fetches tool catalogs from upstream servers at startup
2. Registers proxy tools that forward calls to the appropriate upstream
3. Builds an aggregated search index over all upstream tool catalogs
4. Passes through Bearer tokens for authentication
"""

import asyncio
import fnmatch
import json
import logging
import os
import time
import uuid
from collections import defaultdict
from typing import Any, Optional

import httpx
from fastmcp import Context, FastMCP

from code_execution.activity_publisher import ActivityPublisher
from code_execution.auth.base import AuthConfig
from base import BaseMCPServer
from code_execution.sessions.context import get_current_request_token
from code_execution.tool_registry import ToolDefinition
from code_execution.tools.tool_search import ToolInfo

from .config import ConnectorConfig, UpstreamConfig

LOGGER = logging.getLogger(__name__)


class ConnectorServer(BaseMCPServer):
    """
    Lightweight MCP server that proxies tool calls to upstream domain servers.

    Supports two modes:
    - **Router**: Aggregates tools from multiple upstreams into one MCP endpoint
    - **Gateway**: Proxies a single upstream with governance policies

    The connector has no Python kernel. It proxies `execute_code` calls and
    other MCP tools to upstream servers via HTTP, forwarding the caller's
    Bearer token for authentication.
    """

    def __init__(
        self,
        config: ConnectorConfig,
        auth_config: Optional[AuthConfig] = None,
    ):
        super().__init__()
        self.config = config

        # Upstream state
        self._upstream_catalogs: dict[str, list[ToolDefinition]] = {}
        self._session_mapping: dict[str, dict[str, str]] = {}  # connector_session → {upstream_name: upstream_session}

        # Rate limiting state (gateway mode)
        self._call_timestamps: dict[str, list[float]] = defaultdict(list)

        # Tool search
        self._tool_search_backends: list[Any] = []

        # Activity publisher
        self.activity_publisher = ActivityPublisher(server_name=config.name)

        # Auth config
        if auth_config is None:
            from code_execution.auth import create_noop_auth_config

            auth_config = create_noop_auth_config()
        self.auth_config = auth_config

        # Entra IDs for RFC 9728 metadata
        self.entra_client_id = config.entra_client_id or os.getenv("ENTRA_CLIENT_ID")
        self.entra_tenant_id = config.entra_tenant_id or os.getenv("ENTRA_TENANT_ID")

        # FastMCP instance
        self.mcp = FastMCP(
            f"{config.name}-connector",
            instructions=config.description or f"Connector server ({config.mode} mode) aggregating upstream tools.",
        )

    # ========================================================================
    # Startup
    # ========================================================================

    async def _startup(self):
        """Initialize the connector: fetch upstream catalogs and register proxy tools."""
        LOGGER.info("Initializing connector '%s' in %s mode...", self.config.name, self.config.mode)

        # Fetch catalogs from all upstreams
        await self._sync_upstream_catalogs()

        # Register proxy tools based on mode
        if self.config.mode == "router":
            self._setup_router_tools()
        elif self.config.mode == "gateway":
            self._setup_gateway_tools()

        # Build aggregated search index
        self._setup_search_tool()

        # Start activity publisher
        try:
            await self.activity_publisher.start()
        except Exception:
            LOGGER.warning("ActivityPublisher failed to start; continuing without it", exc_info=True)

        LOGGER.info(
            "Connector '%s' initialization complete. %d upstream(s), %d tool(s) registered.",
            self.config.name,
            len(self._upstream_catalogs),
            sum(len(tools) for tools in self._upstream_catalogs.values()),
        )

    async def _shutdown(self):
        """Clean up resources."""
        LOGGER.info("Shutting down connector '%s'...", self.config.name)
        for backend in self._tool_search_backends:
            if hasattr(backend, "close"):
                try:
                    await backend.close()
                except Exception:
                    LOGGER.warning("Failed to close search backend during shutdown", exc_info=True)
        try:
            await self.activity_publisher.stop()
        except Exception:
            LOGGER.debug("ActivityPublisher stop raised during shutdown; ignoring", exc_info=True)
        LOGGER.info("Connector shutdown complete.")

    # ========================================================================
    # Catalog Sync
    # ========================================================================

    async def _sync_upstream_catalogs(self):
        """Fetch tool catalogs from all configured upstreams."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            tasks = [self._fetch_catalog(client, upstream) for upstream in self.config.upstreams]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        for upstream, result in zip(self.config.upstreams, results):
            if isinstance(result, BaseException):
                LOGGER.error(
                    "Failed to fetch catalog from upstream '%s' at %s: %s",
                    upstream.name,
                    upstream.url,
                    result,
                )
            else:
                tool_list: list[ToolDefinition] = result
                self._upstream_catalogs[upstream.name] = tool_list
                LOGGER.info(
                    "Fetched %d tools from upstream '%s'",
                    len(tool_list),
                    upstream.name,
                )

    async def _fetch_catalog(self, client: httpx.AsyncClient, upstream: UpstreamConfig) -> list[ToolDefinition]:
        """Fetch and parse the tool catalog from a single upstream."""
        url = f"{upstream.url.rstrip('/')}/catalog"
        response = await client.get(url)
        response.raise_for_status()
        data = response.json()

        tools = []
        for tool_data in data.get("tools", []):
            tool_def = ToolDefinition(**tool_data)
            # Tag with server name if not already set
            if not tool_def.server_name:
                tool_def.server_name = data.get("server_name", upstream.name)
            tools.append(tool_def)

        # Apply expose_tools filter
        if upstream.expose_tools and upstream.expose_tools != ["*"]:
            tools = [t for t in tools if self._matches_expose_filter(t.name, upstream.expose_tools)]

        # Apply tool aliases (rename tools for the connector's namespace)
        if upstream.tool_aliases:
            for tool_def in tools:
                if tool_def.name in upstream.tool_aliases:
                    tool_def.name = upstream.tool_aliases[tool_def.name]

        return tools

    @staticmethod
    def _matches_expose_filter(tool_name: str, patterns: list[str]) -> bool:
        """Check if a tool name matches any of the expose_tools glob patterns."""
        return any(fnmatch.fnmatch(tool_name, pattern) for pattern in patterns)

    # ========================================================================
    # Router Mode
    # ========================================================================

    def _setup_router_tools(self):
        """Register proxy tools for each upstream's execute_code and related tools."""
        for upstream in self.config.upstreams:
            tools = self._upstream_catalogs.get(upstream.name, [])
            if not tools:
                continue

            # Register the execute_code proxy for this upstream
            self._register_execute_code_proxy(upstream)

            # Register session management proxies
            self._register_session_proxies(upstream)

    def _register_execute_code_proxy(self, upstream: UpstreamConfig):
        """Register an execute_{upstream.name}_code proxy tool."""
        server = self
        upstream_name = upstream.name

        tool_name = f"execute_{upstream_name}_code"

        async def execute_code_proxy(
            ctx: Context,
            code: str,
            description: str = "",
            timeout: int = 300,
            background: bool = False,
        ) -> str:
            """Execute Python code on the upstream server.

            This call is proxied to the upstream domain server. The code runs
            in the upstream's kernel environment with its domain-specific packages.
            """
            return await server._proxy_mcp_tool_call(
                upstream=upstream,
                tool_name=tool_name,
                arguments={
                    "code": code,
                    "description": description,
                    "timeout": timeout,
                    "background": background,
                },
                ctx=ctx,
            )

        # Build description from upstream catalog
        tools = self._upstream_catalogs.get(upstream_name, [])
        tool_descriptions = [f"  - {t.name}: {t.description}" for t in tools[:10]]
        extra = f"\n  ... and {len(tools) - 10} more" if len(tools) > 10 else ""
        catalog_summary = "\n".join(tool_descriptions) + extra

        description = (
            f"Execute Python code in the {upstream_name} environment (proxied).\n\n"
            f"Available tool functions:\n{catalog_summary}\n\n"
            f"Call list_tools() in your code for full signatures and documentation."
        )

        self.mcp.tool(name=tool_name, description=description)(execute_code_proxy)

    def _register_session_proxies(self, upstream: UpstreamConfig):
        """Register session management proxy tools for an upstream."""
        server = self
        upstream_name = upstream.name
        prefix = upstream_name

        # List sessions
        async def list_sessions_proxy(ctx: Context, summary_only: bool = True) -> str:
            """List active sessions on the upstream server."""
            return await server._proxy_mcp_tool_call(
                upstream=upstream,
                tool_name=f"{prefix}_list_sessions",
                arguments={"summary_only": summary_only},
                ctx=ctx,
            )

        self.mcp.tool(
            name=f"{prefix}_list_sessions",
            description=f"List all active sessions on the {upstream_name} server.",
        )(list_sessions_proxy)

        # Inspect session
        async def inspect_session_proxy(ctx: Context, session_id: str) -> str:
            """Inspect a session on the upstream server."""
            return await server._proxy_mcp_tool_call(
                upstream=upstream,
                tool_name=f"{prefix}_inspect_session",
                arguments={"session_id": session_id},
                ctx=ctx,
            )

        self.mcp.tool(
            name=f"{prefix}_inspect_session",
            description=f"Inspect a session namespace, variable summaries, and background job status on {upstream_name}.",
        )(inspect_session_proxy)

        # Close session
        async def close_session_proxy(ctx: Context, session_id: str) -> str:
            """Close a session on the upstream server."""
            return await server._proxy_mcp_tool_call(
                upstream=upstream,
                tool_name=f"{prefix}_close_session",
                arguments={"session_id": session_id},
                ctx=ctx,
            )

        self.mcp.tool(
            name=f"{prefix}_close_session",
            description=f"Close a session on the {upstream_name} server.",
        )(close_session_proxy)

    # ========================================================================
    # Gateway Mode
    # ========================================================================

    def _setup_gateway_tools(self):
        """Register proxy tools for the single upstream with policy enforcement."""
        if not self.config.upstreams:
            LOGGER.error("Gateway mode requires at least one upstream.")
            return

        upstream = self.config.upstreams[0]
        tools = self._upstream_catalogs.get(upstream.name, [])
        if not tools:
            LOGGER.warning("No tools fetched from upstream '%s' for gateway.", upstream.name)

        # Register execute_code proxy with policy checks
        self._register_gateway_execute_code(upstream)

        # Register session proxies
        self._register_session_proxies(upstream)

    def _register_gateway_execute_code(self, upstream: UpstreamConfig):
        """Register execute_code proxy with gateway policy enforcement."""
        server = self
        upstream_name = upstream.name
        tool_name = f"execute_{upstream_name}_code"
        policy = self.config.gateway_policy

        async def execute_code_gateway(
            ctx: Context,
            code: str,
            description: str = "",
            timeout: int = 300,
            background: bool = False,
        ) -> str:
            """Execute Python code (proxied through gateway with policy enforcement)."""
            # Tool allow/deny policy enforcement
            if policy:
                if policy.blocked_tools:
                    # Check if any blocked tool names appear in the code
                    for blocked in policy.blocked_tools:
                        if blocked in code:
                            return json.dumps(
                                {
                                    "success": False,
                                    "error": f"Tool '{blocked}' is blocked by gateway policy.",
                                }
                            )
                if policy.allowed_tools is not None:
                    # When allowed_tools is set, only allow calls containing those tools
                    # This is a best-effort check on the code content
                    has_allowed = any(tool in code for tool in policy.allowed_tools)
                    if not has_allowed and policy.allowed_tools:
                        return json.dumps(
                            {
                                "success": False,
                                "error": (f"Code does not use any allowed tools. Allowed: {policy.allowed_tools}"),
                            }
                        )

            # Rate limiting
            if policy and policy.max_calls_per_minute:
                user_id = server._get_user_id_from_context()
                if not server._check_rate_limit(user_id, policy.max_calls_per_minute):
                    return json.dumps(
                        {
                            "success": False,
                            "error": (
                                f"Rate limit exceeded: maximum {policy.max_calls_per_minute} "
                                f"calls per minute. Please wait and try again."
                            ),
                        }
                    )

            return await server._proxy_mcp_tool_call(
                upstream=upstream,
                tool_name=tool_name,
                arguments={
                    "code": code,
                    "description": description,
                    "timeout": timeout,
                    "background": background,
                },
                ctx=ctx,
            )

        tools = self._upstream_catalogs.get(upstream_name, [])
        tool_descriptions = [f"  - {t.name}: {t.description}" for t in tools[:10]]
        extra = f"\n  ... and {len(tools) - 10} more" if len(tools) > 10 else ""
        catalog_summary = "\n".join(tool_descriptions) + extra

        description = (
            f"Execute Python code in the {upstream_name} environment (via gateway).\n\n"
            f"Available tool functions:\n{catalog_summary}\n\n"
            f"Call list_tools() in your code for full signatures and documentation."
        )

        self.mcp.tool(name=tool_name, description=description)(execute_code_gateway)

    def _check_rate_limit(self, user_id: str, max_per_minute: int) -> bool:
        """Check if user is within rate limit. Returns True if allowed."""
        now = time.time()
        window_start = now - 60.0

        # Clean old timestamps
        self._call_timestamps[user_id] = [ts for ts in self._call_timestamps[user_id] if ts > window_start]

        if len(self._call_timestamps[user_id]) >= max_per_minute:
            return False

        self._call_timestamps[user_id].append(now)
        return True

    @staticmethod
    def _get_user_id_from_context() -> str:
        """Extract user identity from the current request context."""
        from code_execution.sessions.context import get_current_user_identity

        identity = get_current_user_identity()
        return identity or "anonymous"

    # ========================================================================
    # Search Tool
    # ========================================================================

    def _setup_search_tool(self):
        """Build an aggregated search index over all upstream catalogs."""
        from code_execution.tools import create_tool_search_backend
        from code_execution.tools.tool_search import ToolSearchResult  # noqa: F811

        connector_name = self.config.name
        tool_name = f"search_{connector_name}_tools"

        # Build ToolInfo list from all upstream catalogs
        all_tool_infos: list[ToolInfo] = []
        for upstream_name, tools in self._upstream_catalogs.items():
            for td in tools:
                all_tool_infos.append(
                    ToolInfo(
                        name=td.name,
                        description=td.description,
                        server_name=td.server_name or upstream_name,
                        affordances=tuple(td.affordances),
                        state_requires=tuple(sorted(td.state_transition.requires)),
                        state_produces=tuple(sorted(td.state_transition.produces)),
                    )
                )

        if not all_tool_infos:
            LOGGER.debug("No tools to index for connector '%s'; skipping search tool.", connector_name)
            return

        backend = create_tool_search_backend(backend_type="bm25")
        backend.index(tools=all_tool_infos, skills=[], server_name=connector_name)
        self._tool_search_backends.append(backend)

        LOGGER.info(
            "Connector search index built for '%s' with %d tools from %d upstream(s).",
            connector_name,
            len(all_tool_infos),
            len(self._upstream_catalogs),
        )

        async def search_connector_tools(
            query: str, top: int = 5, category: str = "all", ctx: Optional[Context] = None
        ) -> str:
            """Search the aggregated tool catalog from all upstream servers.

            Args:
                query: Natural-language description or tool name to search for.
                    Pass an empty string with ``top=999`` to retrieve the full catalog.
                top: Maximum number of results to return per category (default 5).
                category: Filter results — ``"all"`` (default), ``"tools"``, or ``"skills"``.

            Returns:
                JSON object with ``tools`` and ``skills`` arrays.
            """
            if category not in ("all", "tools", "skills"):
                return json.dumps({"error": f"Invalid category '{category}'. Must be 'all', 'tools', or 'skills'."})
            try:
                results: list[ToolSearchResult] = await backend.search(query, top, category=category)
                tools_list = [r.model_dump() for r in results if r.type == "tool"]
                skills_list = [r.model_dump() for r in results if r.type == "skill"]
                return json.dumps({"tools": tools_list, "skills": skills_list})
            except Exception as exc:
                LOGGER.error("search_%s_tools failed: %s", connector_name, exc, exc_info=True)
                return json.dumps({"tools": [], "skills": [], "error": f"{type(exc).__name__}: {exc}"})

        self.mcp.tool(
            name=tool_name,
            description=(
                f"Search {connector_name} domain tools and skills by name or description. "
                f"Returns matching results grouped into 'tools' and 'skills' arrays. "
                f"Use category='skills' to find only skills, or category='tools' for only tools."
            ),
        )(search_connector_tools)

    # ========================================================================
    # MCP Proxy Infrastructure
    # ========================================================================

    async def _proxy_mcp_tool_call(
        self,
        upstream: UpstreamConfig,
        tool_name: str,
        arguments: dict[str, Any],
        ctx: Optional[Context] = None,
    ) -> str:
        """Proxy an MCP tool call to an upstream server.

        Forwards the caller's Bearer token and MCP session context for
        authentication and session continuity.
        """
        upstream_url = upstream.url.rstrip("/")

        # Get the caller's Bearer token to forward.
        # Note: in background tasks ContextVars may not be inherited;
        # callers that need guaranteed auth should ensure context propagation.
        request_token = get_current_request_token()
        if not request_token:
            LOGGER.warning(
                "No request token available for proxy call to '%s' tool '%s'. Auth pass-through will be skipped.",
                upstream.name,
                tool_name,
            )

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if request_token:
            headers["Authorization"] = f"Bearer {request_token}"

        # Forward MCP session ID if available (enables session-scoped behavior on upstream)
        if ctx:
            try:
                session_id = ctx.session_id
                if session_id:
                    headers["Mcp-Session-Id"] = session_id
            except (RuntimeError, AttributeError) as exc:
                LOGGER.debug(
                    "Skipping MCP session ID forwarding for upstream '%s' tool '%s': %s",
                    upstream.name,
                    tool_name,
                    exc,
                )

        # Build MCP tool call request (JSON-RPC 2.0) with unique request ID
        request_id = str(uuid.uuid4())
        mcp_request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments,
            },
        }

        try:
            async with httpx.AsyncClient(timeout=600.0) as client:
                # Initialize MCP session if needed
                response = await client.post(
                    f"{upstream_url}/mcp",
                    json=mcp_request,
                    headers=headers,
                )
                response.raise_for_status()

                result = response.json()

                # Extract result from JSON-RPC response
                if "error" in result:
                    error = result["error"]
                    return json.dumps(
                        {
                            "success": False,
                            "error": f"Upstream error: {error.get('message', str(error))}",
                        }
                    )

                # MCP tool results come as content array
                mcp_result = result.get("result", {})
                content = mcp_result.get("content", [])

                # Extract text content
                text_parts = []
                for item in content:
                    if item.get("type") == "text":
                        text_parts.append(item.get("text", ""))

                return "\n".join(text_parts) if text_parts else json.dumps(mcp_result)

        except httpx.HTTPStatusError as exc:
            LOGGER.error(
                "Upstream '%s' returned HTTP %d for tool '%s': %s",
                upstream.name,
                exc.response.status_code,
                tool_name,
                exc.response.text[:500],
            )
            return json.dumps(
                {
                    "success": False,
                    "error": f"Upstream '{upstream.name}' returned HTTP {exc.response.status_code}",
                }
            )
        except httpx.RequestError as exc:
            LOGGER.error(
                "Failed to reach upstream '%s' at %s: %s",
                upstream.name,
                upstream_url,
                exc,
            )
            return json.dumps(
                {
                    "success": False,
                    "error": f"Cannot reach upstream '{upstream.name}': {type(exc).__name__}: {exc}",
                }
            )

    # ========================================================================
    # BaseMCPServer abstract implementations
    # ========================================================================

    async def _health_payload(self) -> dict[str, Any]:
        """Return health check payload with upstream status."""
        upstream_status = {}
        for upstream in self.config.upstreams:
            has_tools = upstream.name in self._upstream_catalogs
            upstream_status[upstream.name] = "connected" if has_tools else "unavailable"
        return {
            "status": "healthy",
            "connector": self.config.name,
            "mode": self.config.mode,
            "upstreams": upstream_status,
        }

    async def _catalog_payload(self) -> dict[str, Any]:
        """Return the aggregated tool catalog from all upstreams."""
        all_tools = []
        for tools in self._upstream_catalogs.values():
            all_tools.extend([t.model_dump(mode="json") for t in tools])
        return {
            "server_name": self.config.name,
            "tools": all_tools,
        }

    def _extract_user_identity(self, token_data: dict) -> Optional[str]:
        """Extract user identity using the configured identity extractor."""
        return self.auth_config.identity_extractor.extract(token_data)
