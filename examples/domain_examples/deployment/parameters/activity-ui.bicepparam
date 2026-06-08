// Parameter file for the activity-ui deployment.
//
// Infrastructure values (environmentId, identityId, registryServer, etc.)
// are passed by deploy.sh from .env.server — do NOT duplicate them here.
//
// Usage:
//   ./deploy.sh --server activity-ui --template activity-ui.bicep \
//     --dockerfile activity_ui/Dockerfile

using '../activity-ui.bicep'

// ── Container configuration ─────────────────────────────────────────────────

param containerPort = 8030
param cpu          = '0.25'
param memory       = '0.5Gi'
