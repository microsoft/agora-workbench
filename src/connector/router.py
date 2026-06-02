"""
RouterServer — aggregates tools from multiple upstream servers.

Presents a unified MCP endpoint with execute_code and session management
tools for each upstream, plus a combined search index.
"""

import logging
from typing import Optional

from fastmcp import Context

from code_execution.auth.base import AuthConfig

from .base import ConnectorServer
from .models import RouterConfig, UpstreamConfig

LOGGER = logging.getLogger(__name__)


class RouterServer(ConnectorServer):
    """Connector that aggregates tools from multiple upstreams into one MCP endpoint.

    Each upstream gets its own ``execute_{name}_code`` tool plus session
    management proxies. A unified search tool covers all upstreams.

    Example::

        config = RouterConfig(
            name="science-hub",
            upstreams=[
                UpstreamConfig(name="chemistry", url="http://chemistry:8000"),
                UpstreamConfig(name="gis", url="http://gis:8000"),
            ],
        )
        server = RouterServer(config)
        await server.run_http(port=9000)
    """

    def __init__(self, config: RouterConfig, auth_config: Optional[AuthConfig] = None):
        self.config = config
        super().__init__(
            name=config.name,
            description=config.description,
            upstreams=config.upstreams,
            entra_client_id=config.entra_client_id,
            entra_tenant_id=config.entra_tenant_id,
            auth_config=auth_config,
        )

    def _setup_tools(self) -> None:
        """Register execute_code and session proxies for each upstream."""
        for upstream in self._upstreams:
            tools = self._upstream_catalogs.get(upstream.name, [])
            if not tools:
                continue
            self._register_execute_code_proxy(upstream)
            self._register_session_proxies(upstream)
            self._register_check_job_proxy(upstream)
            self._register_parallel_execution_proxies(upstream)
            self._register_publish_artifact_proxy(upstream)
            self._register_push_object_proxy(upstream)
            self._register_workflow_proxies(upstream)

    def _register_execute_code_proxy(self, upstream: UpstreamConfig) -> None:
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
            """Execute Python code on the upstream server (proxied)."""
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

        desc = self._build_catalog_description(upstream_name)
        self.mcp.tool(name=tool_name, description=desc)(execute_code_proxy)
