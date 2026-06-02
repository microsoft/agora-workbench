"""
GatewayServer — proxies a single upstream with policy enforcement.

Adds rate limiting, tool allow/deny lists, and audit logging on top
of the upstream's tools.
"""

import json
import logging
import time
from collections import defaultdict
from typing import Optional

from fastmcp import Context

from code_execution.auth.base import AuthConfig

from .base import ConnectorServer
from .models import GatewayConfig

LOGGER = logging.getLogger(__name__)


class GatewayServer(ConnectorServer):
    """Connector that proxies a single upstream with governance policies.

    Enforces rate limits, tool allow/deny lists before forwarding calls.

    Example::

        config = GatewayConfig(
            name="chem-gateway",
            upstream=UpstreamConfig(name="chemistry", url="http://chemistry:8000"),
            policy=GatewayPolicy(max_calls_per_minute=60, blocked_tools=["parallel_execute"]),
        )
        server = GatewayServer(config)
        await server.run_http(port=9000)
    """

    def __init__(self, config: GatewayConfig, auth_config: Optional[AuthConfig] = None):
        self.config = config
        self._call_timestamps: dict[str, list[float]] = defaultdict(list)
        super().__init__(
            name=config.name,
            description=config.description,
            upstreams=[config.upstream],
            entra_client_id=config.entra_client_id,
            entra_tenant_id=config.entra_tenant_id,
            auth_config=auth_config,
        )

    def _setup_tools(self) -> None:
        """Register execute_code proxy with policy enforcement + session proxies."""
        upstream = self.config.upstream
        tools = self._upstream_catalogs.get(upstream.name, [])
        if not tools:
            LOGGER.warning("No tools fetched from upstream '%s' for gateway.", upstream.name)

        self._register_gateway_execute_code(upstream)
        self._register_session_proxies(upstream)

        # Register companion proxy tools, respecting blocked_tools policy.
        # Blocking "parallel_execute" also suppresses check_batch/cancel_batch
        # since those are only meaningful alongside parallel execution.
        blocked = set(self.config.policy.blocked_tools)

        if "check_job" not in blocked:
            self._register_check_job_proxy(upstream)

        if "parallel_execute" not in blocked:
            self._register_parallel_execution_proxies(upstream)

        if "publish_artifact" not in blocked:
            self._register_publish_artifact_proxy(upstream)

        if "push_object" not in blocked:
            self._register_push_object_proxy(upstream)

        if "plan_workflow" not in blocked:
            self._register_plan_workflow_proxy(upstream)

        if "load_skill" not in blocked:
            self._register_load_skill_proxy(upstream)

    def _register_gateway_execute_code(self, upstream) -> None:
        """Register execute_code with policy checks (rate limit, allow/deny)."""
        server = self
        upstream_name = upstream.name
        tool_name = f"execute_{upstream_name}_code"
        policy = self.config.policy

        async def execute_code_gateway(
            ctx: Context,
            code: str,
            description: str = "",
            timeout: int = 300,
            background: bool = False,
        ) -> str:
            """Execute Python code (proxied through gateway with policy enforcement)."""
            # Tool allow/deny policy enforcement
            if policy.blocked_tools:
                for blocked in policy.blocked_tools:
                    if blocked in code:
                        return json.dumps(
                            {"success": False, "error": f"Tool '{blocked}' is blocked by gateway policy."}
                        )

            if policy.allowed_tools is not None:
                has_allowed = any(tool in code for tool in policy.allowed_tools)
                if not has_allowed and policy.allowed_tools:
                    return json.dumps(
                        {
                            "success": False,
                            "error": f"Code does not use any allowed tools. Allowed: {policy.allowed_tools}",
                        }
                    )

            # Rate limiting
            if policy.max_calls_per_minute:
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

        desc = self._build_catalog_description(upstream_name, prefix="via gateway, ")
        self.mcp.tool(name=tool_name, description=desc)(execute_code_gateway)

    def _check_rate_limit(self, user_id: str, max_per_minute: int) -> bool:
        """Check if user is within rate limit. Returns True if allowed."""
        now = time.time()
        window_start = now - 60.0

        self._call_timestamps[user_id] = [ts for ts in self._call_timestamps[user_id] if ts > window_start]

        if len(self._call_timestamps[user_id]) >= max_per_minute:
            return False

        self._call_timestamps[user_id].append(now)
        return True
