"""
Configuration models for connector servers.
"""

from typing import Optional

from pydantic import BaseModel, Field


class UpstreamConfig(BaseModel):
    """Configuration for a single upstream server that the connector proxies to."""

    name: str = Field(description="Logical name for this upstream (e.g., 'chemistry', 'gis')")
    url: str = Field(description="Base URL of the upstream server (e.g., 'http://chemistry:8000')")
    expose_tools: Optional[list[str]] = Field(
        default=None,
        description=(
            "Glob patterns for tools to expose from this upstream. "
            "None or ['*'] means expose all tools. "
            "Examples: ['compute_*', 'cluster_molecules']"
        ),
    )
    tool_aliases: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Rename tools when exposing them through the connector. "
            "Maps upstream tool name → connector-exposed name. "
            "Example: {'compute_descriptors': 'chem_compute_descriptors'}"
        ),
    )


class GatewayPolicy(BaseModel):
    """Policy configuration for GatewayServer."""

    allowed_tools: Optional[list[str]] = Field(
        default=None,
        description="Tool names allowed through the gateway. None means all allowed.",
    )
    blocked_tools: list[str] = Field(
        default_factory=list,
        description="Tool names blocked by the gateway.",
    )
    max_calls_per_minute: Optional[int] = Field(
        default=None,
        ge=1,
        description="Rate limit: maximum tool calls per minute per user. None means unlimited.",
    )


class RouterConfig(BaseModel):
    """Configuration for a RouterServer.

    A router aggregates tools from multiple upstream servers into a single
    MCP endpoint, providing unified tool discovery and proxied execution.
    """

    name: str = Field(description="Server name (e.g., 'science-hub')")
    description: str = Field(
        default="",
        description="Human-readable description (used as MCP instructions).",
    )
    upstreams: list[UpstreamConfig] = Field(
        description="Upstream servers to aggregate (one or more).",
        min_length=1,
    )
    entra_client_id: Optional[str] = Field(
        default=None,
        description="Entra ID application client ID for this server's app registration.",
    )
    entra_tenant_id: Optional[str] = Field(
        default=None,
        description="Azure AD tenant ID for this server's app registration.",
    )


class GatewayConfig(BaseModel):
    """Configuration for a GatewayServer.

    A gateway proxies a single upstream server with policy enforcement:
    rate limiting, tool allow/deny lists, and audit logging.
    """

    name: str = Field(description="Server name (e.g., 'chem-gateway')")
    description: str = Field(
        default="",
        description="Human-readable description (used as MCP instructions).",
    )
    upstream: UpstreamConfig = Field(
        description="The single upstream server to proxy.",
    )
    policy: GatewayPolicy = Field(
        default_factory=GatewayPolicy,
        description="Governance policy for the gateway.",
    )
    entra_client_id: Optional[str] = Field(
        default=None,
        description="Entra ID application client ID for this server's app registration.",
    )
    entra_tenant_id: Optional[str] = Field(
        default=None,
        description="Azure AD tenant ID for this server's app registration.",
    )


# Keep ConnectorConfig as a backwards-compatible alias during migration
ConnectorConfig = RouterConfig
