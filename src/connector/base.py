"""
ConnectorServer — abstract base for lightweight MCP proxy servers.

Connectors have no Python kernel. They fetch tool catalogs from upstream
servers, register proxy tools, and forward MCP calls with auth pass-through.
"""

import asyncio
import fnmatch
import json
import logging
import os
import uuid
from abc import abstractmethod
from typing import Any, Optional

import httpx
from fastmcp import Context, FastMCP

from base import BaseMCPServer
from code_execution.activity_publisher import ActivityPublisher
from code_execution.auth.base import AuthConfig
from code_execution.sessions.context import get_current_request_token
from code_execution.tool_registry import ToolDefinition
from code_execution.tools.tool_search import ToolInfo

from .models import UpstreamConfig

LOGGER = logging.getLogger(__name__)


class ConnectorServer(BaseMCPServer):
    """Abstract base for connector servers (router, gateway, etc.).

    Provides shared infrastructure:
    - Upstream catalog fetching and caching
    - MCP tool call proxying with auth pass-through
    - Aggregated tool search index
    - Activity publishing

    Subclasses implement ``_setup_tools()`` to register their mode-specific
    proxy tools and ``_get_upstreams()`` to declare their upstream list.
    """

    def __init__(
        self,
        *,
        name: str,
        description: str = "",
        upstreams: list[UpstreamConfig],
        entra_client_id: Optional[str] = None,
        entra_tenant_id: Optional[str] = None,
        auth_config: Optional[AuthConfig] = None,
    ):
        super().__init__()
        self._server_name = name
        self._description = description
        self._upstreams = upstreams

        # Upstream state
        self._upstream_catalogs: dict[str, list[ToolDefinition]] = {}

        # Tool search
        self._tool_search_backends: list[Any] = []

        # Activity publisher
        self.activity_publisher = ActivityPublisher(server_name=name)

        # Auth config
        if auth_config is None:
            from code_execution.auth import create_noop_auth_config

            auth_config = create_noop_auth_config()
        self.auth_config = auth_config

        # Entra IDs for RFC 9728 metadata
        self.entra_client_id = entra_client_id or os.getenv("ENTRA_CLIENT_ID")
        self.entra_tenant_id = entra_tenant_id or os.getenv("ENTRA_TENANT_ID")

        # FastMCP instance
        self.mcp = FastMCP(
            f"{name}-connector",
            instructions=description or "Connector server aggregating upstream tools.",
        )

    # ========================================================================
    # Abstract: subclass hook for tool registration
    # ========================================================================

    @abstractmethod
    def _setup_tools(self) -> None:
        """Register proxy tools specific to this connector mode.

        Called after catalogs are synced but before the search tool is built.
        """
        ...

    # ========================================================================
    # Lifecycle
    # ========================================================================

    async def _startup(self) -> None:
        """Initialize the connector: fetch catalogs, register tools, build search."""
        LOGGER.info("Initializing connector '%s'...", self._server_name)

        await self._sync_upstream_catalogs()

        self._setup_tools()
        self._setup_search_tool()

        try:
            await self.activity_publisher.start()
        except Exception:
            LOGGER.warning("ActivityPublisher failed to start; continuing without it", exc_info=True)

        LOGGER.info(
            "Connector '%s' ready. %d upstream(s), %d tool(s).",
            self._server_name,
            len(self._upstream_catalogs),
            sum(len(tools) for tools in self._upstream_catalogs.values()),
        )

    async def _shutdown(self) -> None:
        """Clean up resources."""
        LOGGER.info("Shutting down connector '%s'...", self._server_name)
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

    async def _sync_upstream_catalogs(self) -> None:
        """Fetch tool catalogs from all configured upstreams."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            tasks = [self._fetch_catalog(client, upstream) for upstream in self._upstreams]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        for upstream, result in zip(self._upstreams, results):
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
                LOGGER.info("Fetched %d tools from upstream '%s'", len(tool_list), upstream.name)

    async def _fetch_catalog(self, client: httpx.AsyncClient, upstream: UpstreamConfig) -> list[ToolDefinition]:
        """Fetch and parse the tool catalog from a single upstream."""
        url = f"{upstream.url.rstrip('/')}/catalog"
        response = await client.get(url)
        response.raise_for_status()
        data = response.json()

        tools = []
        for tool_data in data.get("tools", []):
            tool_def = ToolDefinition(**tool_data)
            if not tool_def.server_name:
                tool_def.server_name = data.get("server_name", upstream.name)
            tools.append(tool_def)

        # Apply expose_tools filter
        if upstream.expose_tools and upstream.expose_tools != ["*"]:
            tools = [t for t in tools if self._matches_expose_filter(t.name, upstream.expose_tools)]

        # Apply tool aliases
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
    # Session Proxies (shared by router and gateway)
    # ========================================================================

    def _register_session_proxies(self, upstream: UpstreamConfig) -> None:
        """Register session management proxy tools for an upstream."""
        server = self
        upstream_name = upstream.name
        prefix = upstream_name

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
    # Search Tool
    # ========================================================================

    def _setup_search_tool(self) -> None:
        """Build an aggregated search index over all upstream catalogs."""
        from code_execution.tools import create_tool_search_backend
        from code_execution.tools.tool_search import ToolSearchResult

        connector_name = self._server_name
        tool_name = f"search_{connector_name}_tools"

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
            "Search index built for '%s': %d tools from %d upstream(s).",
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
                top: Maximum number of results to return per category (default 5).
                category: Filter — "all" (default), "tools", or "skills".

            Returns:
                JSON with ``tools`` and ``skills`` arrays.
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

        Forwards the caller's Bearer token and MCP session context.
        """
        upstream_url = upstream.url.rstrip("/")

        request_token = get_current_request_token()
        if not request_token:
            LOGGER.warning(
                "No request token for proxy call to '%s' tool '%s'. Auth pass-through skipped.",
                upstream.name,
                tool_name,
            )

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if request_token:
            headers["Authorization"] = f"Bearer {request_token}"

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

        request_id = str(uuid.uuid4())
        mcp_request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }

        try:
            async with httpx.AsyncClient(timeout=600.0) as client:
                response = await client.post(f"{upstream_url}/mcp", json=mcp_request, headers=headers)
                response.raise_for_status()
                result = response.json()

                if "error" in result:
                    error = result["error"]
                    return json.dumps(
                        {"success": False, "error": f"Upstream error: {error.get('message', str(error))}"}
                    )

                mcp_result = result.get("result", {})
                content = mcp_result.get("content", [])
                text_parts = [item.get("text", "") for item in content if item.get("type") == "text"]
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
                {"success": False, "error": f"Upstream '{upstream.name}' returned HTTP {exc.response.status_code}"}
            )
        except httpx.RequestError as exc:
            LOGGER.error("Failed to reach upstream '%s' at %s: %s", upstream.name, upstream_url, exc)
            return json.dumps(
                {"success": False, "error": f"Cannot reach upstream '{upstream.name}': {type(exc).__name__}: {exc}"}
            )

    # ========================================================================
    # BaseMCPServer abstract implementations
    # ========================================================================

    async def _health_payload(self) -> dict[str, Any]:
        """Return health check payload with upstream status."""
        upstream_status = {}
        for upstream in self._upstreams:
            has_tools = upstream.name in self._upstream_catalogs
            upstream_status[upstream.name] = "connected" if has_tools else "unavailable"
        return {
            "status": "healthy",
            "server": self._server_name,
            "type": type(self).__name__,
            "upstreams": upstream_status,
        }

    async def _catalog_payload(self) -> dict[str, Any]:
        """Return the aggregated tool catalog from all upstreams."""
        all_tools = []
        for tools in self._upstream_catalogs.values():
            all_tools.extend([t.model_dump(mode="json") for t in tools])
        return {"server_name": self._server_name, "tools": all_tools}

    def _extract_user_identity(self, token_data: dict) -> Optional[str]:
        """Extract user identity using the configured identity extractor."""
        return self.auth_config.identity_extractor.extract(token_data)

    # ========================================================================
    # Helpers
    # ========================================================================

    @staticmethod
    def _get_user_id_from_context() -> str:
        """Extract user identity from the current request context."""
        from code_execution.sessions.context import get_current_user_identity

        identity = get_current_user_identity()
        return identity or "anonymous"

    def _build_catalog_description(self, upstream_name: str, prefix: str = "") -> str:
        """Build a human-readable tool catalog summary for an upstream."""
        tools = self._upstream_catalogs.get(upstream_name, [])
        tool_descriptions = [f"  - {t.name}: {t.description}" for t in tools[:10]]
        extra = f"\n  ... and {len(tools) - 10} more" if len(tools) > 10 else ""
        catalog_summary = "\n".join(tool_descriptions) + extra
        return (
            f"Execute Python code in the {upstream_name} environment ({prefix}proxied).\n\n"
            f"Available tool functions:\n{catalog_summary}\n\n"
            f"Call list_tools() in your code for full signatures and documentation."
        )
