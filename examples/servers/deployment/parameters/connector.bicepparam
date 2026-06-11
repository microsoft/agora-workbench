// Parameter template for a connector-server deployment.
//
// Infrastructure values (environmentId, identityId, registryServer, etc.)
// are passed by deploy.sh from .env — do NOT duplicate them here.
//
// Usage:
//   ./deploy.sh --server science-hub --dockerfile <path/to/connector/Dockerfile> --context <path/to/connector>

using '../main.bicep'

// ── Server identity ─────────────────────────────────────────────────────────

param serverName    = 'science-hub'
param containerPort = 8000

// ── Resource sizing (connector is stateless and lightweight) ────────────────

param cpu         = '0.5'
param memory      = '1Gi'
param minReplicas = 1
param maxReplicas = 10

// ── Connector mode + upstream mapping ───────────────────────────────────────
// Replace values below with your upstream URLs inside the ACA environment.

param extraEnvVars = {
  CONNECTOR_MODE: 'router'
  UPSTREAM_CHEMISTRY_URL: 'http://chemistry-server:8000/mcp'
  UPSTREAM_GIS_URL: 'http://gis-server:8000/mcp'
}
