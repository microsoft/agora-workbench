"""
Configuration models for connector servers.
"""

from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator


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


class BridgeEdge(BaseModel):
    """A synthetic cross-server edge in the unified state graph.

    Bridges declare that reaching ``from_state`` on one upstream enables
    transitioning to ``to_state`` on another. They are injected into the
    hub-level :class:`~code_execution.tools.search.state_graph.StateGraph`
    as navigation aids — they do not gate execution.
    """

    from_state: str = Field(
        description=(
            "Source state token (e.g. 'graphormer.reduction_predicted'). "
            "Must exist in an upstream tool's state_produces."
        )
    )
    to_state: str = Field(
        description=(
            "Target state token (e.g. 'ezbattery.electrolyte_configured'). "
            "Must exist in an upstream tool's state_requires."
        )
    )
    description: str = Field(
        default="",
        description="Human-readable note explaining what data/context flows across this bridge.",
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
    bridges: list[BridgeEdge] = Field(
        default_factory=list,
        description=(
            "Cross-server bridge edges for the unified state graph. "
            "Each bridge declares a navigation link between states on different upstreams. "
            "Validated at startup against the aggregated catalog."
        ),
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


class WorkerConfig(BaseModel):
    """Configuration for a single worker in a dispatcher pool."""

    name: str = Field(description="Logical name for this worker (e.g., 'chem-worker-1')")
    url: str = Field(description="Base URL of the worker server (e.g., 'http://chemistry-1:8000')")
    weight: int = Field(
        default=1,
        ge=1,
        description="Routing weight for weighted round-robin. Higher weight = more traffic.",
    )


class DispatcherConfig(BaseModel):
    """Configuration for a DispatcherServer.

    A dispatcher fans out a single tool interface to a pool of identical
    worker servers, with configurable routing strategies and health checking.
    """

    name: str = Field(description="Server name (e.g., 'chem-dispatcher')")
    description: str = Field(
        default="",
        description="Human-readable description (used as MCP instructions).",
    )
    workers: list[WorkerConfig] = Field(
        description="Pool of identical worker servers to distribute across.",
        min_length=1,
    )
    strategy: Literal["round_robin", "least_loaded", "sticky_session"] = Field(
        default="round_robin",
        description="Routing strategy for distributing calls across workers.",
    )
    session_affinity: bool = Field(
        default=True,
        description=(
            "If True, once a session is assigned to a worker, subsequent calls "
            "in that session route to the same worker."
        ),
    )
    health_check_interval: float = Field(
        default=10.0,
        gt=0,
        description="Seconds between health check polls to each worker.",
    )
    worker_failure_policy: Literal["error", "reroute"] = Field(
        default="error",
        description=(
            "Behavior when an assigned worker goes unhealthy mid-session. "
            "'error' returns an error to the caller; 'reroute' assigns a new worker."
        ),
    )
    entra_client_id: Optional[str] = Field(
        default=None,
        description="Entra ID application client ID for this server's app registration.",
    )
    entra_tenant_id: Optional[str] = Field(
        default=None,
        description="Azure AD tenant ID for this server's app registration.",
    )

    @model_validator(mode="after")
    def _validate_sticky_requires_affinity(self) -> "DispatcherConfig":
        if self.strategy == "sticky_session" and not self.session_affinity:
            raise ValueError("strategy='sticky_session' requires session_affinity=True")
        return self


# Keep ConnectorConfig as a backwards-compatible alias during migration
ConnectorConfig = RouterConfig
