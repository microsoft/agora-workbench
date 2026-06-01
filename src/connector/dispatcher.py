"""
DispatcherServer — fans out a single tool interface to a pool of identical workers.

Distributes execute_code calls across workers using configurable routing
strategies (round_robin, least_loaded, sticky_session) with health-aware
routing and session affinity.
"""

import asyncio
import json
import logging
from typing import Any, Optional

import httpx
from fastmcp import Context

from code_execution.auth.base import AuthConfig
from code_execution.sessions.context import get_current_request_token

from .base import ConnectorServer
from .models import DispatcherConfig, UpstreamConfig, WorkerConfig

LOGGER = logging.getLogger(__name__)


class DispatcherServer(ConnectorServer):
    """Connector that distributes calls across a pool of identical workers.

    All workers run the same environment and expose the same tools. The
    dispatcher presents a single unified tool interface and routes calls
    using the configured strategy.

    Example::

        config = DispatcherConfig(
            name="chem-dispatcher",
            workers=[
                WorkerConfig(name="chem-worker-1", url="http://chemistry-1:8000"),
                WorkerConfig(name="chem-worker-2", url="http://chemistry-2:8000"),
            ],
            strategy="round_robin",
            session_affinity=True,
        )
        server = DispatcherServer(config)
        await server.run_http(port=9000)
    """

    def __init__(self, config: DispatcherConfig, auth_config: Optional[AuthConfig] = None):
        self.config = config

        # Convert WorkerConfigs to UpstreamConfigs for base class
        upstreams = [UpstreamConfig(name=w.name, url=w.url) for w in config.workers]

        super().__init__(
            name=config.name,
            description=config.description,
            upstreams=upstreams,
            entra_client_id=config.entra_client_id,
            entra_tenant_id=config.entra_tenant_id,
            auth_config=auth_config,
        )

        # Routing state
        self._worker_configs: dict[str, WorkerConfig] = {w.name: w for w in config.workers}
        self._healthy_workers: set[str] = {w.name for w in config.workers}
        self._round_robin_index: int = 0
        self._active_calls: dict[str, int] = {w.name: 0 for w in config.workers}
        self._session_affinity_map: dict[str, str] = {}  # connector_session_id -> worker_name

        # Per-session upstream MCP sessions: (connector_session_id, worker_name) -> upstream_session_id
        self._dispatcher_sessions: dict[tuple[str, str], str] = {}

        # Routing lock
        self._routing_lock = asyncio.Lock()

        # Health check
        self._health_check_task: Optional[asyncio.Task] = None

        # Discovered upstream tool name (from catalog)
        self._upstream_execute_tool_name: Optional[str] = None

    # ========================================================================
    # Lifecycle
    # ========================================================================

    async def _startup(self) -> None:
        """Initialize dispatcher: fetch catalog, register tools, start health checks."""
        LOGGER.info("Initializing dispatcher '%s'...", self._server_name)

        await self._sync_dispatcher_catalog()

        self._setup_tools()
        self._setup_search_tool()

        try:
            await self.activity_publisher.start()
        except Exception:
            LOGGER.warning("ActivityPublisher failed to start; continuing without it", exc_info=True)

        # Start background health check
        self._health_check_task = asyncio.create_task(self._health_check_loop())

        LOGGER.info(
            "Dispatcher '%s' ready. %d worker(s), strategy=%s.",
            self._server_name,
            len(self.config.workers),
            self.config.strategy,
        )

    async def _shutdown(self) -> None:
        """Clean up resources and cancel health checks."""
        LOGGER.info("Shutting down dispatcher '%s'...", self._server_name)

        if self._health_check_task and not self._health_check_task.done():
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass

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

        LOGGER.info("Dispatcher shutdown complete.")

    # ========================================================================
    # Catalog Sync (from first healthy worker)
    # ========================================================================

    async def _sync_dispatcher_catalog(self) -> None:
        """Fetch tool catalog from the first reachable worker.

        All workers are identical so we only need one catalog. We try each
        worker in order until one responds successfully.
        """
        async with httpx.AsyncClient(timeout=30.0) as client:
            for worker in self.config.workers:
                upstream = UpstreamConfig(name=worker.name, url=worker.url)
                try:
                    tools = await self._fetch_catalog(client, upstream)
                    # Store catalog under all worker names (same tools)
                    for w in self.config.workers:
                        self._upstream_catalogs[w.name] = tools
                    self._discover_execute_tool_name(tools)
                    LOGGER.info(
                        "Fetched catalog from worker '%s': %d tools",
                        worker.name,
                        len(tools),
                    )
                    return
                except Exception as exc:
                    LOGGER.warning(
                        "Failed to fetch catalog from worker '%s': %s. Trying next.",
                        worker.name,
                        exc,
                    )
                    self._healthy_workers.discard(worker.name)

        LOGGER.error("Failed to fetch catalog from any worker for dispatcher '%s'", self._server_name)

    def _discover_execute_tool_name(self, tools) -> None:
        """Find the execute_code-like tool from the catalog."""
        from code_execution.tool_registry import ToolDefinition

        for tool in tools:
            if isinstance(tool, ToolDefinition) and "execute" in tool.name and "code" in tool.name:
                self._upstream_execute_tool_name = tool.name
                return
        # Fallback: use first tool with "execute" in name, or just "execute_code"
        for tool in tools:
            if isinstance(tool, ToolDefinition) and "execute" in tool.name:
                self._upstream_execute_tool_name = tool.name
                return
        self._upstream_execute_tool_name = "execute_code"

    # ========================================================================
    # Tool Registration
    # ========================================================================

    def _setup_tools(self) -> None:
        """Register the single execute_code proxy tool."""
        self._register_dispatcher_execute_code()

    def _register_dispatcher_execute_code(self) -> None:
        """Register the unified execute_code tool that routes to workers."""
        server = self
        tool_name = "execute_code"

        # Build description from catalog
        first_worker = self.config.workers[0].name
        tools = self._upstream_catalogs.get(first_worker, [])
        tool_descriptions = [f"  - {t.name}: {t.description}" for t in tools[:10]]
        extra = f"\n  ... and {len(tools) - 10} more" if len(tools) > 10 else ""
        catalog_summary = "\n".join(tool_descriptions) + extra

        desc = (
            f"Execute Python code (dispatched across {len(self.config.workers)} workers).\n\n"
            f"Available tool functions:\n{catalog_summary}\n\n"
            f"Call list_tools() in your code for full signatures and documentation."
        )

        async def execute_code_dispatcher(
            ctx: Context,
            code: str,
            description: str = "",
            timeout: int = 300,
            background: bool = False,
        ) -> str:
            """Execute Python code on a worker (dispatched)."""
            connector_session_id = _get_session_id_from_context(ctx)

            # Select worker
            worker_name = await server._select_worker(connector_session_id)
            if worker_name is None:
                return json.dumps(
                    {
                        "success": False,
                        "error": "No healthy workers available in the dispatcher pool.",
                    }
                )

            # Get upstream config for this worker
            upstream = server._get_upstream_for_worker(worker_name)

            # Track active calls for least_loaded
            async with server._routing_lock:
                server._active_calls[worker_name] = server._active_calls.get(worker_name, 0) + 1

            try:
                result = await server._proxy_dispatcher_call(
                    upstream=upstream,
                    tool_name=server._upstream_execute_tool_name or "execute_code",
                    arguments={
                        "code": code,
                        "description": description,
                        "timeout": timeout,
                        "background": background,
                    },
                    ctx=ctx,
                    connector_session_id=connector_session_id,
                )
                return result
            except Exception as exc:
                # Mark worker unhealthy on connection failure
                if isinstance(exc, (httpx.RequestError, httpx.HTTPStatusError)):
                    await server._mark_worker_unhealthy(worker_name, connector_session_id)
                LOGGER.error("Dispatcher call to worker '%s' failed: %s", worker_name, exc)
                return json.dumps(
                    {
                        "success": False,
                        "error": f"Worker '{worker_name}' failed: {type(exc).__name__}: {exc}",
                    }
                )
            finally:
                async with server._routing_lock:
                    server._active_calls[worker_name] = max(0, server._active_calls.get(worker_name, 1) - 1)

        self.mcp.tool(name=tool_name, description=desc)(execute_code_dispatcher)

    # ========================================================================
    # Routing
    # ========================================================================

    async def _select_worker(self, connector_session_id: str) -> Optional[str]:
        """Select a worker based on the configured strategy."""
        async with self._routing_lock:
            # Check session affinity first
            if self.config.session_affinity and connector_session_id in self._session_affinity_map:
                assigned = self._session_affinity_map[connector_session_id]
                if assigned in self._healthy_workers:
                    return assigned
                # Assigned worker is unhealthy
                if self.config.worker_failure_policy == "error":
                    return None
                # reroute: clear affinity and pick a new worker
                del self._session_affinity_map[connector_session_id]
                LOGGER.warning(
                    "Worker '%s' unhealthy for session '%s'; rerouting.",
                    assigned,
                    connector_session_id[:12],
                )

            if not self._healthy_workers:
                return None

            worker_name = self._pick_worker_by_strategy()

            # Record affinity
            if self.config.session_affinity:
                self._session_affinity_map[connector_session_id] = worker_name

            return worker_name

    def _pick_worker_by_strategy(self) -> str:
        """Pick a worker using the configured strategy. Must be called under lock."""
        healthy = [w for w in self.config.workers if w.name in self._healthy_workers]
        if not healthy:
            raise RuntimeError("No healthy workers")  # Should not happen; caller checks

        if self.config.strategy == "round_robin":
            return self._pick_round_robin(healthy)
        elif self.config.strategy == "least_loaded":
            return self._pick_least_loaded(healthy)
        elif self.config.strategy == "sticky_session":
            # Sticky session without existing affinity -> round robin for initial assignment
            return self._pick_round_robin(healthy)
        else:
            return self._pick_round_robin(healthy)

    def _pick_round_robin(self, healthy: list[WorkerConfig]) -> str:
        """Weighted round-robin selection."""
        # Build weighted list
        weighted: list[str] = []
        for w in healthy:
            weighted.extend([w.name] * w.weight)

        if not weighted:
            return healthy[0].name

        self._round_robin_index = self._round_robin_index % len(weighted)
        selected = weighted[self._round_robin_index]
        self._round_robin_index = (self._round_robin_index + 1) % len(weighted)
        return selected

    def _pick_least_loaded(self, healthy: list[WorkerConfig]) -> str:
        """Select worker with fewest active calls."""
        return min(healthy, key=lambda w: self._active_calls.get(w.name, 0)).name

    # ========================================================================
    # Proxy (dispatcher-specific session handling)
    # ========================================================================

    async def _proxy_dispatcher_call(
        self,
        upstream: UpstreamConfig,
        tool_name: str,
        arguments: dict[str, Any],
        ctx: Optional[Context],
        connector_session_id: str,
    ) -> str:
        """Proxy an MCP tool call to a worker with per-session upstream sessions."""
        upstream_url = upstream.url.rstrip("/")

        request_token = get_current_request_token()
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream, application/json",
        }
        if request_token:
            headers["Authorization"] = f"Bearer {request_token}"

        # Session keyed by (connector_session, worker)
        session_key = (connector_session_id, upstream.name)
        try:
            upstream_session_id = await self._ensure_dispatcher_session(upstream, headers, session_key)
            headers["Mcp-Session-Id"] = upstream_session_id
        except Exception as exc:
            LOGGER.error("Failed to establish session with worker '%s': %s", upstream.name, exc)
            return json.dumps(
                {
                    "success": False,
                    "error": f"Cannot establish session with worker '{upstream.name}': {exc}",
                }
            )

        import uuid

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

                # If session expired, retry with fresh session
                if response.status_code in (400, 404):
                    LOGGER.info(
                        "Worker session may have expired for '%s', re-establishing...",
                        upstream.name,
                    )
                    self._dispatcher_sessions.pop(session_key, None)
                    upstream_session_id = await self._ensure_dispatcher_session(upstream, headers, session_key)
                    headers["Mcp-Session-Id"] = upstream_session_id
                    response = await client.post(f"{upstream_url}/mcp", json=mcp_request, headers=headers)

                response.raise_for_status()

                result = self._parse_mcp_response(response)
                if "error" in result:
                    error = result["error"]
                    return json.dumps(
                        {
                            "success": False,
                            "error": f"Worker error: {error.get('message', str(error))}",
                        }
                    )

                mcp_result = result.get("result", {})
                content = mcp_result.get("content", [])
                text_parts = [item.get("text", "") for item in content if item.get("type") == "text"]
                return "\n".join(text_parts) if text_parts else json.dumps(mcp_result)

        except httpx.HTTPStatusError as exc:
            # Mark unhealthy reactively
            await self._mark_worker_unhealthy(upstream.name, connector_session_id)
            LOGGER.error(
                "Worker '%s' returned HTTP %d: %s",
                upstream.name,
                exc.response.status_code,
                exc.response.text[:500],
            )
            return json.dumps(
                {
                    "success": False,
                    "error": f"Worker '{upstream.name}' returned HTTP {exc.response.status_code}",
                }
            )
        except httpx.RequestError as exc:
            await self._mark_worker_unhealthy(upstream.name, connector_session_id)
            LOGGER.error("Failed to reach worker '%s' at %s: %s", upstream.name, upstream_url, exc)
            return json.dumps(
                {
                    "success": False,
                    "error": f"Cannot reach worker '{upstream.name}': {type(exc).__name__}: {exc}",
                }
            )

    async def _ensure_dispatcher_session(
        self,
        upstream: UpstreamConfig,
        headers: dict[str, str],
        session_key: tuple[str, str],
    ) -> str:
        """Ensure an upstream MCP session exists for this connector session + worker pair."""
        if session_key in self._dispatcher_sessions:
            return self._dispatcher_sessions[session_key]

        import uuid

        upstream_url = upstream.url.rstrip("/")
        init_headers = {
            **headers,
            "Accept": "text/event-stream, application/json",
        }
        init_request = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": f"{self._server_name}-dispatcher", "version": "1.0"},
            },
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{upstream_url}/mcp", json=init_request, headers=init_headers)
            resp.raise_for_status()
            session_id = resp.headers.get("mcp-session-id")
            if not session_id:
                raise RuntimeError(f"Worker '{upstream.name}' did not return Mcp-Session-Id on initialize")

            # Send initialized notification
            notif_headers = {**init_headers, "Mcp-Session-Id": session_id}
            await client.post(
                f"{upstream_url}/mcp",
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers=notif_headers,
            )

        self._dispatcher_sessions[session_key] = session_id
        LOGGER.info(
            "Established MCP session with worker '%s' for connector session '%s': %s",
            upstream.name,
            session_key[0][:12],
            session_id[:12],
        )
        return session_id

    # ========================================================================
    # Health Checking
    # ========================================================================

    async def _health_check_loop(self) -> None:
        """Periodically poll worker health endpoints."""
        interval = self.config.health_check_interval
        while True:
            try:
                await asyncio.sleep(interval)
                await self._poll_worker_health()
            except asyncio.CancelledError:
                break
            except Exception:
                LOGGER.warning("Health check loop error", exc_info=True)

    async def _poll_worker_health(self) -> None:
        """Check /health on all workers and update healthy set."""
        async with httpx.AsyncClient(timeout=5.0) as client:
            tasks = [self._check_worker_health(client, worker) for worker in self.config.workers]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        async with self._routing_lock:
            for worker, result in zip(self.config.workers, results):
                if isinstance(result, BaseException) or result is False:
                    if worker.name in self._healthy_workers:
                        LOGGER.warning("Worker '%s' marked unhealthy.", worker.name)
                        self._healthy_workers.discard(worker.name)
                else:
                    if worker.name not in self._healthy_workers:
                        LOGGER.info("Worker '%s' recovered, marked healthy.", worker.name)
                        self._healthy_workers.add(worker.name)

    async def _check_worker_health(self, client: httpx.AsyncClient, worker: WorkerConfig) -> bool:
        """Check a single worker's health. Returns True if healthy."""
        url = f"{worker.url.rstrip('/')}/health"
        try:
            resp = await client.get(url)
            return resp.status_code == 200
        except httpx.RequestError:
            return False

    async def _mark_worker_unhealthy(self, worker_name: str, connector_session_id: str) -> None:
        """Reactively mark a worker as unhealthy after a failed call."""
        async with self._routing_lock:
            if worker_name in self._healthy_workers:
                LOGGER.warning(
                    "Worker '%s' marked unhealthy (reactive, session '%s').",
                    worker_name,
                    connector_session_id[:12],
                )
                self._healthy_workers.discard(worker_name)

    # ========================================================================
    # Helpers
    # ========================================================================

    def _get_upstream_for_worker(self, worker_name: str) -> UpstreamConfig:
        """Get the UpstreamConfig for a given worker name."""
        worker = self._worker_configs[worker_name]
        return UpstreamConfig(name=worker.name, url=worker.url)


def _get_session_id_from_context(ctx: Optional[Context]) -> str:
    """Extract the inbound MCP session ID from the FastMCP context."""
    if ctx is not None:
        try:
            session_id = ctx.session_id
            if session_id:
                return str(session_id)
        except (AttributeError, Exception):
            pass
    # Fallback: generate a unique ID (no affinity possible)
    import uuid

    return str(uuid.uuid4())
