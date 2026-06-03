// Parameter template for a dispatcher-mode connector deployment.
//
// Infrastructure values (environmentId, identityId, registryServer, etc.)
// are passed by deploy.sh from .env — do NOT duplicate them here.
//
// Usage:
//   ./deploy-network.sh networks/chem-dispatcher.yaml

using '../main.bicep'

// ── Server identity ─────────────────────────────────────────────────────────

param serverName    = 'chem-dispatcher'
param containerPort = 8000

// ── Resource sizing (dispatcher is stateless and lightweight) ───────────────

param cpu         = '0.5'
param memory      = '1Gi'
param minReplicas = 1
param maxReplicas = 1

// ── Dispatcher mode + worker mapping ────────────────────────────────────────
// Replace values below with your worker URLs inside the ACA environment.

param extraEnvVars = {
  CONNECTOR_MODE: 'dispatcher'
  WORKER_CHEM1_URL: 'http://chem-worker-1:8000'
  WORKER_CHEM2_URL: 'http://chem-worker-2:8000'
  DISPATCHER_STRATEGY: 'round_robin'
  DISPATCHER_HEALTH_CHECK_INTERVAL: '10'
  DISPATCHER_FAILURE_POLICY: 'error'
}
