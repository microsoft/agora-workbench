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
- **Background jobs** — `check_job` for polling long-running executions (created by `async_only` or `adaptive` execution modes)
- **Parallel execution** — `parallel_execute`, `check_batch`, `cancel_batch`
- **Unified send** — `{name}_send` for data transfer (to peer servers, blob storage, user download, or local filesystem)
- **Workflow planning** — `plan_workflow` and `load_skill` per upstream
- **Unified workflow planning** — `plan_{router_name}_workflow` across all upstreams (with optional bridge edges)

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

### Unified state graph and bridge edges

When upstream tools declare `requires`/`produces` state transitions, the router builds a **unified state graph** spanning all upstreams and exposes a `plan_{router_name}_workflow` tool. By default, each upstream's states form disconnected islands. To enable cross-server path queries, declare **bridge edges** in the router config:

```python
from agora_workbench.connector import RouterServer, BridgeEdge
from agora_workbench.connector.models import RouterConfig, UpstreamConfig

config = RouterConfig(
    name="science-hub",
    upstreams=[
        UpstreamConfig(name="graphormer", url="http://graphormer:8000"),
        UpstreamConfig(name="ezbattery", url="http://ezbattery:8000"),
    ],
    bridges=[
        BridgeEdge(
            from_state="graphormer.reduction_predicted",
            to_state="ezbattery.electrolyte_configured",
            description="Pass predicted potentials to battery simulation",
        ),
    ],
)

server = RouterServer(config)
```

The agent can now query `plan_science-hub_workflow(mode="path", current_state="graphormer.reduction_predicted", target_state="ezbattery.simulation_complete")` and get a cross-server path that traverses the bridge.

**Key design points:**

- **Bridges are a navigation aid** — they help agents discover multi-server workflows at cold-start. They do not gate execution or move data automatically.
- **Hub-side overlay only** — bridges exist only in the router's composed graph. Each upstream's own `plan_{upstream}_workflow` remains self-consistent and unchanged.
- **Validated at startup** — both `from_state` and `to_state` must exist in the aggregated upstream catalogs. Invalid bridges fail loudly with a `ValueError`.
- **Tagged in output** — bridge steps appear with `server="(bridge)"` and a `bridge:` name prefix, so agents can distinguish them from real tool transitions.
- **OR semantics** — `requires` means "reachable from any one of these states". A bridge saying "A enables B" does not express AND-conjunctions; that would need a fused precondition state.

**YAML configuration** (equivalent):

```yaml
name: science-hub
upstreams:
  - name: graphormer
    url: http://graphormer:8000
  - name: ezbattery
    url: http://ezbattery:8000
bridges:
  - from_state: graphormer.reduction_predicted
    to_state: ezbattery.electrolyte_configured
    description: Pass predicted potentials to battery simulation
```

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

## Server-to-server transfers (the peer registry)

`{name}_send(to="<peer>")` pushes a Python object from one server's kernel
directly into a peer server's kernel — useful when one domain produces input for
another (e.g. build a network in `energysystems`, then render it in
`earthscience`). This is a direct kernel-to-kernel transfer and does **not**
require a `ConnectorServer`.

For a server to be a valid `to=` destination, the **operator** must register it
in the sender's **peer registry** — a single `name → base URL` map. You maintain
one table instead of hand-wiring a publisher per pair (O(N) config, not O(N²)).

### Configuring the registry

Two sources, merged (the env var takes precedence); the server always drops its
own name:

**1. In `ServerConfig`:**

```python
config = ServerConfig(
    name="energysystems",
    # ...
    peer_registry={"earthscience": "https://earthscience.internal:8000"},
)
```

**2. Via the `AGORA_PEER_REGISTRY` env var** — inline JSON, or a path to a JSON
file. One shared file can be handed to every server (each ignores its own
entry), so the whole mesh is described in one place:

```bash
AGORA_PEER_REGISTRY='{"chemistry":"http://chemistry-server:8000","earthscience":"http://earthscience-server:8000","energysystems":"http://energysystems-server:8000"}'
```

The URL is the server **root** (where `/object-transfer/receive` lives), not the
`/mcp` endpoint.

### What the agent sees

The agent calls `energysystems_send(data_ref="grid", to="earthscience")` with a
**logical name**, never a URL — so it can only reach operator-registered peers.
Registered peers appear in the send tool's own description; an unregistered `to=`
returns `Unknown destination '<name>'`.

### Security and transport

- Only operator-registered names are reachable (an allow-list — the agent cannot
  push to arbitrary hosts).
- HTTPS is required for non-loopback hosts in general. **Registry peers are
  exempt**: because the operator chose the scheme directly in the registry URL,
  a `http://` peer is trusted as-is — you do **not** also need to list it in
  `OBJECT_TRANSFER_TRUSTED_HTTP_HOSTS`. (That env var still governs plain-HTTP
  transfers that don't go through the registry, e.g. statically pre-registered
  publishers.)
- The `OBJECT_TRANSFER_ALLOWED_HOSTS` SSRF allow-list, when set, still applies
  to registry peers.
- The caller's bearer token is forwarded over the link, so peers must share an
  auth realm. Prefer HTTPS for any non-loopback peer so the token is not sent
  over an unencrypted connection.

### Runtime requirements

These are properties of the push itself, not the registry:

- The **receiving** server must already have an active session for the caller —
  have the agent run `execute_<target>_code` on the target once before sending,
  or the receive endpoint returns 404.
- Both servers must be live at transfer time.
- There is a maximum transfer size; route large datasets through blob storage
  (`to="blob"`) instead of a direct push.

### Example

The bundled `chemistry`, `earthscience`, and `energysystems` example servers
share one topology table in `examples/servers/peers.py` (localhost defaults for
bare-host dev) and override it for the dockerised deployment via
`AGORA_PEER_REGISTRY` in each `docker-compose.yml`.

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
