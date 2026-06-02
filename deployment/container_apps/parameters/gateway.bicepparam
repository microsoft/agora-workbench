// Parameter template for a gateway connector deployment.
//
// A gateway proxies a single upstream (typically a router) with policy
// enforcement: rate limiting, tool allow/deny lists.
//
// Usage:
//   ./deploy.sh --network networks/science-hub-gateway.yaml

using '../main.bicep'

// ── Server identity ─────────────────────────────────────────────────────────

param serverName    = 'science-gateway'
param containerPort = 8000

// ── Resource sizing (connector is stateless and lightweight) ────────────────

param cpu         = '0.5'
param memory      = '1Gi'
param minReplicas = 1
param maxReplicas = 10

// ── Connector configuration ─────────────────────────────────────────────────
// Gateway mode requires exactly one UPSTREAM_*_URL.

param extraEnvVars = {
  CONNECTOR_MODE: 'gateway'
  CONNECTOR_NAME: 'science-gateway'
  UPSTREAM_ROUTER_URL: 'http://science-router:8000/mcp'
  GATEWAY_BLOCKED_TOOLS: 'parallel_execute'
  GATEWAY_MAX_CALLS_PER_MINUTE: '60'
}
