// Parameter template for a router connector deployment.
//
// A router aggregates tools from multiple upstream MCP servers into one
// unified endpoint. Set one UPSTREAM_<NAME>_URL per upstream server.
//
// Usage:
//   ./deploy.sh --network networks/science-hub-gateway.yaml

using '../main.bicep'

// ── Server identity ─────────────────────────────────────────────────────────

param serverName    = 'science-router'
param containerPort = 8000

// ── Resource sizing (connector is stateless and lightweight) ────────────────

param cpu         = '0.5'
param memory      = '1Gi'
param minReplicas = 1
param maxReplicas = 10

// ── Connector configuration ─────────────────────────────────────────────────

param extraEnvVars = {
  CONNECTOR_MODE: 'router'
  CONNECTOR_NAME: 'science-router'
  UPSTREAM_CHEMISTRY_URL: 'http://chemistry-server:8000/mcp'
  UPSTREAM_EARTHSCIENCE_URL: 'http://earthscience-server:8000/mcp'
}
