"""
Connector infrastructure for composing multiple MCP servers.

Connectors are lightweight MCP servers that proxy tool calls to upstream
domain servers without running their own Python kernels. They enable:

- **RouterServer**: Aggregate tools from multiple upstreams into one MCP endpoint
- **GatewayServer**: Proxy a single upstream with governance policies (rate limits, allow/deny)
"""

from .base import ConnectorServer
from .gateway import GatewayServer
from .models import GatewayConfig, GatewayPolicy, RouterConfig, UpstreamConfig
from .router import RouterServer

__all__ = [
    "ConnectorServer",
    "GatewayConfig",
    "GatewayPolicy",
    "GatewayServer",
    "RouterConfig",
    "RouterServer",
    "UpstreamConfig",
]
