"""
Configuration models for ConnectorServer.
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
    """Policy configuration for gateway mode."""

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


class ConnectorConfig(BaseModel):
    """Configuration for a ConnectorServer instance.

    A connector is a lightweight MCP server that proxies tool calls to
    upstream domain servers. It has no Python kernel or execution environment
    of its own.
    """

    name: str = Field(description="Connector server name (e.g., 'science-hub')")
    mode: Literal["router", "gateway"] = Field(
        description=(
            "Connector mode. "
            "'router': aggregates tools from multiple upstreams. "
            "'gateway': proxies a single upstream with policy enforcement."
        )
    )
    description: str = Field(
        default="",
        description="Human-readable description of the connector (used as MCP instructions).",
    )
    upstreams: list[UpstreamConfig] = Field(
        description="Upstream servers to connect to.",
    )
    gateway_policy: Optional[GatewayPolicy] = Field(
        default=None,
        description="Policy configuration (only used in gateway mode).",
    )
    entra_client_id: Optional[str] = Field(
        default=None,
        description="Entra ID application client ID for this connector's app registration.",
    )
    entra_tenant_id: Optional[str] = Field(
        default=None,
        description="Azure AD tenant ID for this connector's app registration.",
    )

    @model_validator(mode="after")
    def _validate_gateway_single_upstream(self) -> "ConnectorConfig":
        """Gateway mode requires exactly one upstream."""
        if self.mode == "gateway" and len(self.upstreams) != 1:
            raise ValueError("Gateway mode requires exactly one upstream, got %d." % len(self.upstreams))
        return self
