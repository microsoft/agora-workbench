// Parameter file for your server deployment.
//
// Infrastructure values (environmentId, identityId, registryServer, etc.)
// are passed by deploy.sh from .env — do NOT duplicate them here.
//
// Usage:
//   ./deploy.sh --server <your-server-name>

using '../main.bicep'

// ── Server identity ─────────────────────────────────────────────────────────

param serverName    = 'my-server'
param containerPort = 8000

// ── Resource sizing (ACA consumption plan) ──────────────────────────────────

param cpu         = '2.0'
param memory      = '4Gi'
param minReplicas = 1
param maxReplicas = 3
