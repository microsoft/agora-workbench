"""
Connector infrastructure for composing multiple MCP servers.

Connectors are lightweight MCP servers that proxy tool calls to upstream
domain servers without running their own Python kernels. They enable:

- **Router**: Aggregate tools from multiple upstreams into one MCP endpoint
- **Gateway**: Proxy a single upstream with governance policies (rate limits, allow/deny)
"""

from .config import ConnectorConfig, GatewayPolicy, UpstreamConfig
from .server import ConnectorServer

__all__ = [
    "ConnectorConfig",
    "ConnectorServer",
    "GatewayPolicy",
    "UpstreamConfig",
]
