# Server networks with ConnectorServer

When you have multiple `CodeExecutionServer` instances (e.g., chemistry, GIS, energy), you can compose them behind a `ConnectorServer` to present a unified MCP endpoint to the agent.

## Architecture

```
Agent
  │
  ▼
ConnectorServer (Router, Gateway, or Dispatcher)
  │
  ├── CodeExecutionServer (chemistry)
  ├── CodeExecutionServer (gis)
  └── CodeExecutionServer (energysystems)
```

The `ConnectorServer` is a lightweight proxy — it has no Python kernel of its own. It fetches tool catalogs from upstream servers, registers proxy tools, and forwards MCP calls with authentication pass-through.

### Proxied capabilities

Connectors provide full parity with direct upstream access. All of the following are proxied transparently:

- **Code execution** — `execute_{name}_code`
- **Session management** — list, inspect, close sessions
- **Background jobs** — `check_job` for polling long-running executions
- **Parallel execution** — `parallel_execute`, `check_batch`, `cancel_batch`
- **Artifact publishing** — `publish_artifact` to remote storage
- **Object transfer** — `push_object` for server-to-server variable passing
- **Workflow planning** — `plan_workflow` and `load_skill` per upstream

## RouterServer

A `RouterServer` aggregates tools from multiple upstreams into a single MCP endpoint. Each upstream gets its own `execute_{name}_code` tool plus session management proxies:

```python
import asyncio
from agora_workbench.connector import RouterServer
from agora_workbench.connector.models import RouterConfig, UpstreamConfig

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
- A unified `search_science-hub_tools` that covers all upstreams

## GatewayServer

A `GatewayServer` proxies a **single** upstream with policy enforcement — rate limiting, tool allow/deny lists, and audit logging:

```python
from agora_workbench.connector import GatewayServer
from agora_workbench.connector.models import GatewayConfig, GatewayPolicy, UpstreamConfig

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

## DispatcherServer

A `DispatcherServer` fans out a single tool interface to a **pool of identical workers**. Use it when you need horizontal scaling — multiple replicas of the same `CodeExecutionServer` behind a load-balancing proxy that understands MCP sessions.

```python
from agora_workbench.connector import DispatcherServer
from agora_workbench.connector.models import DispatcherConfig, WorkerConfig

config = DispatcherConfig(
    name="chem-dispatcher",
    workers=[
        WorkerConfig(name="chem-worker-1", url="http://chemistry-1:8000"),
        WorkerConfig(name="chem-worker-2", url="http://chemistry-2:8000"),
        WorkerConfig(name="chem-worker-3", url="http://chemistry-3:8000", weight=2),
    ],
    strategy="round_robin",
    session_affinity=True,
    health_check_interval=10.0,
    worker_failure_policy="reroute",
)

server = DispatcherServer(config)

if __name__ == "__main__":
    asyncio.run(server.run_http(host="0.0.0.0", port=9000))
```

The agent sees a single `execute_code` tool — the dispatcher routes each call to a healthy worker transparently.

### Routing strategies

| Strategy | Behavior |
|----------|----------|
| `round_robin` | Weighted round-robin across healthy workers. Use `WorkerConfig.weight` to bias traffic. |
| `least_loaded` | Routes to the worker with the fewest active calls. Best for uneven workloads. |
| `sticky_session` | Initial assignment via round-robin, then sticky (equivalent to `round_robin` + `session_affinity=True`). |

### Session affinity

When `session_affinity=True` (the default), once a session is assigned to a worker, all subsequent calls in that session route to the same worker. This ensures session state (variables, files) remains accessible.

### Worker failure handling

| Policy | Behavior |
|--------|----------|
| `error` | If the assigned worker goes unhealthy mid-session, return an error to the caller. |
| `reroute` | Assign a new healthy worker and continue. Session state on the failed worker is lost. |

### Health checking

The dispatcher polls each worker's `/health` endpoint at the configured interval (default: 10s). Workers that fail health checks are removed from the routing pool. When they recover, they're automatically added back.

Workers are also marked unhealthy reactively if a proxied call fails with a connection error or HTTP 5xx.

### Worker weights

Use `WorkerConfig.weight` to send proportionally more traffic to higher-capacity workers:

```python
workers=[
    WorkerConfig(name="large-worker", url="http://large:8000", weight=3),
    WorkerConfig(name="small-worker", url="http://small:8000", weight=1),
]
```

This sends roughly 75% of new sessions to `large-worker` and 25% to `small-worker`.

### Dispatcher vs. Router

| | Router | Dispatcher |
|---|---|---|
| Workers are... | Different servers (chemistry, GIS, etc.) | Identical replicas of the same server |
| Agent sees... | Multiple `execute_{name}_code` tools | Single `execute_code` tool |
| Use case | Multi-domain | Horizontal scaling |

## When to use which

| Scenario | Use |
|----------|-----|
| Multi-domain agent needs all servers | `RouterServer` |
| Single server needs governance/rate limiting | `GatewayServer` |
| Horizontal scaling of one domain | `DispatcherServer` |
| Team-specific access control | `GatewayServer` per team |
| High-availability with failover | `DispatcherServer` with `worker_failure_policy="reroute"` |

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
# docker-compose.yml — Router example
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

```yaml
# docker-compose.yml — Dispatcher example
services:
  chemistry-1:
    image: myregistry/chemistry-server:latest

  chemistry-2:
    image: myregistry/chemistry-server:latest

  chemistry-3:
    image: myregistry/chemistry-server:latest

  dispatcher:
    image: myregistry/dispatcher-server:latest
    ports: ["9000:9000"]
    environment:
      WORKER_URLS: "http://chemistry-1:8000,http://chemistry-2:8000,http://chemistry-3:8000"
```
