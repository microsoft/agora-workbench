// Parameter file for the energysystems-server deployment.
//
// Infrastructure values (environmentId, identityId, registryServer, etc.)
// are passed by deploy.sh from .env — do NOT duplicate them here.
//
// Usage:
//   ./deploy.sh --server energysystems

using '../main.bicep'

// ── Server identity ─────────────────────────────────────────────────────────

param serverName    = 'energysystems'
param containerPort = 8000

// ── Resource sizing (ACA consumption plan) ──────────────────────────────────

param cpu         = '1.0'
param memory      = '2Gi'
param minReplicas = 1
param maxReplicas = 3
