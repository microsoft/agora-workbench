# Server networks with ConnectorServer

When you have multiple `CodeExecutionServer` instances (e.g., chemistry, GIS, energy), you can compose them behind a `ConnectorServer` to present a unified MCP endpoint to the agent.

## Architecture

```
Agent
  │
  ▼
ConnectorServer (Router or Gateway)
  │
  ├── CodeExecutionServer (chemistry)
  ├── CodeExecutionServer (gis)
  └── CodeExecutionServer (energysystems)
```

The `ConnectorServer` is a lightweight proxy — it has no Python kernel of its own. It fetches tool catalogs from upstream servers, registers proxy tools, and forwards MCP calls with authentication pass-through.

## RouterServer

A `RouterServer` aggregates tools from multiple upstreams into a single MCP endpoint. Each upstream gets its own `execute_{name}_code` tool plus session management proxies:

```python
import asyncio
from connector import RouterServer
from connector.models import RouterConfig, UpstreamConfig

config = RouterConfig(
    name="science-hub",
    upstreams=[
        UpstreamConfig(name="chemistry", url="http://chemistry:8000"),
        UpstreamConfig(name="gis", url="http://gis:8000"),
        UpstreamConfig(name="energy", url="http://energy:8000"),
    ],
)

server = RouterServer(config)

if __name__ == "__main__":
    asyncio.run(server.run_http(host="0.0.0.0", port=9000))
```

The agent sees:

- `execute_chemistry_code`
- `execute_gis_code`
- `execute_energy_code`
- A unified `search_tools` that covers all upstreams

## GatewayServer

A `GatewayServer` proxies a **single** upstream with policy enforcement — rate limiting, tool allow/deny lists, and audit logging:

```python
from connector import GatewayServer
from connector.models import GatewayConfig, GatewayPolicy, UpstreamConfig

config = GatewayConfig(
    name="chem-gateway",
    upstream=UpstreamConfig(name="chemistry", url="http://chemistry:8000"),
    policy=GatewayPolicy(
        max_calls_per_minute=60,
        blocked_tools=["parallel_execute"],
    ),
)

server = GatewayServer(config)
```

### Gateway policies

| Policy | Description |
|--------|-------------|
| `max_calls_per_minute` | Rate limit per user |
| `blocked_tools` | Tools that cannot be called through this gateway |
| `allowed_tools` | Allowlist (if set, only these tools are forwarded) |

## When to use which

| Scenario | Use |
|----------|-----|
| Multi-domain agent needs all servers | `RouterServer` |
| Single server needs governance/rate limiting | `GatewayServer` |
| Team-specific access control | `GatewayServer` per team |
| Staging environment proxy | `GatewayServer` with strict policy |

## Authentication pass-through

Connector servers pass the user's bearer token to upstream servers. Configure Entra ID on the connector:

```python
config = RouterConfig(
    name="science-hub",
    upstreams=[...],
    entra_client_id="<your-app-client-id>",
    entra_tenant_id="<your-tenant-id>",
)
```

Each upstream server validates the token independently. See [Authentication options](authentication.md) for details.

## Deployment

In a Docker Compose or Kubernetes deployment, each server runs as a separate container. The connector is just another container with network access to the upstreams:

```yaml
# docker-compose.yml
services:
  chemistry:
    image: myregistry/chemistry-server:latest
    ports: ["8001:8000"]

  gis:
    image: myregistry/gis-server:latest
    ports: ["8002:8000"]

  router:
    image: myregistry/router-server:latest
    ports: ["9000:9000"]
    environment:
      UPSTREAM_CHEMISTRY_URL: http://chemistry:8000
      UPSTREAM_GIS_URL: http://gis:8000
```
