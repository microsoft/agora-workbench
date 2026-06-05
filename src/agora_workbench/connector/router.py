"""
RouterServer — aggregates tools from multiple upstream servers.

Presents a unified MCP endpoint with execute_code and session management
tools for each upstream, plus a combined search index.
"""

import json
import logging
from typing import Optional

from fastmcp import Context

from agora_workbench.code_execution.auth.base import AuthConfig

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
            # Skip upstreams whose catalog fetch failed (key absent), but
            # still register for upstreams that succeeded with empty results
            # (they still support execute_code and meta-tools).
            if upstream.name not in self._upstream_catalogs:
                continue
            self._register_execute_code_proxy(upstream)
            self._register_session_proxies(upstream)
            self._register_check_job_proxy(upstream)
            self._register_parallel_execution_proxies(upstream)
            self._register_publish_artifact_proxy(upstream)
            self._register_push_object_proxy(upstream)
            self._register_plan_workflow_proxy(upstream)
            self._register_load_skill_proxy(upstream)

        # Unified skill loader across all upstreams
        self._register_unified_load_skill()

    def _register_unified_load_skill(self) -> None:
        """Register a router-level load_skill that routes to the correct upstream."""
        server = self
        router_name = self._server_name
        tool_name = f"load_{router_name}_skill"

        # Build skill-name → upstream mapping. Track duplicates.
        skill_to_upstream: dict[str, UpstreamConfig] = {}
        skill_duplicates: dict[str, list[str]] = {}

        for upstream in self._upstreams:
            for skill in self._upstream_skills.get(upstream.name, []):
                name = skill["name"]
                if name in skill_to_upstream:
                    if name not in skill_duplicates:
                        skill_duplicates[name] = [skill_to_upstream[name].name]
                    skill_duplicates[name].append(upstream.name)
                else:
                    skill_to_upstream[name] = upstream

        if not skill_to_upstream:
            return

        # Build an upstream lookup by name for disambiguation
        upstream_by_name = {u.name: u for u in self._upstreams}

        async def load_skill_unified(ctx: Context, skill_name: str, upstream: str = "") -> str:
            """Load a skill by name from the appropriate upstream server.

            Args:
                skill_name: Name of the skill to load.
                upstream: Optional upstream name to disambiguate when multiple
                    upstreams expose a skill with the same name.
            """
            # If upstream is explicitly specified, route directly
            if upstream:
                target = upstream_by_name.get(upstream)
                if not target:
                    available = sorted(upstream_by_name.keys())
                    return json.dumps(
                        {
                            "error": f"Unknown upstream '{upstream}'.",
                            "available_upstreams": available,
                        }
                    )
                return await server._proxy_mcp_tool_call(
                    upstream=target,
                    tool_name=f"load_{upstream}_skill",
                    arguments={"skill_name": skill_name},
                    ctx=ctx,
                )

            # Check for duplicates
            if skill_name in skill_duplicates:
                return json.dumps(
                    {
                        "error": (
                            f"Skill '{skill_name}' exists on multiple upstreams: "
                            f"{skill_duplicates[skill_name]}. "
                            f"Specify the 'upstream' parameter to disambiguate."
                        ),
                        "matches": [{"skill_name": skill_name, "upstream": u} for u in skill_duplicates[skill_name]],
                    }
                )

            # Look up the owning upstream
            target = skill_to_upstream.get(skill_name)
            if not target:
                available = sorted(skill_to_upstream.keys())
                return json.dumps(
                    {
                        "error": f"Skill '{skill_name}' not found.",
                        "available_skills": available,
                    }
                )

            return await server._proxy_mcp_tool_call(
                upstream=target,
                tool_name=f"load_{target.name}_skill",
                arguments={"skill_name": skill_name},
                ctx=ctx,
            )

        self.mcp.tool(
            name=tool_name,
            description=(
                f"Load the full content of a skill by name. Skills contain step-by-step "
                f"instructions for domain workflows. Discover skill names via "
                f"search_{router_name}_tools(category='skills') or plan_*_workflow. "
                f"If a skill name exists on multiple upstreams, specify the 'upstream' "
                f"parameter to disambiguate."
            ),
        )(load_skill_unified)

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
