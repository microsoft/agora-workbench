"""
Connector infrastructure for composing multiple MCP servers.

Connectors are lightweight MCP servers that proxy tool calls to upstream
domain servers without running their own Python kernels. They enable:

- **RouterServer**: Aggregate tools from multiple upstreams into one MCP endpoint
- **GatewayServer**: Proxy a single upstream with governance policies (rate limits, allow/deny)
- **DispatcherServer**: Fan out a single tool interface to a pool of identical workers
"""

from .base import ConnectorServer
from .dispatcher import DispatcherServer
from .gateway import GatewayServer
from .models import (
    DispatcherConfig,
    GatewayConfig,
    GatewayPolicy,
    RouterConfig,
    UpstreamConfig,
    WorkerConfig,
)
from .router import RouterServer

__all__ = [
    "ConnectorServer",
    "DispatcherConfig",
    "DispatcherServer",
    "GatewayConfig",
    "GatewayPolicy",
    "GatewayServer",
    "RouterConfig",
    "RouterServer",
    "UpstreamConfig",
    "WorkerConfig",
]
