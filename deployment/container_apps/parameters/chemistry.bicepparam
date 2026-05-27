// Parameter file for the chemistry-server deployment.
//
// Infrastructure values (environmentId, identityId, registryServer, etc.)
// are passed by deploy.sh from .env — do NOT duplicate them here.
//
// Usage:
//   ./deploy.sh --server chemistry

using '../main.bicep'

// ── Server identity ─────────────────────────────────────────────────────────

param serverName    = 'chemistry'
param containerPort = 8000

// ── Resource sizing (ACA consumption plan) ──────────────────────────────────

param cpu         = '1.0'
param memory      = '2Gi'
param minReplicas = 1
param maxReplicas = 3
