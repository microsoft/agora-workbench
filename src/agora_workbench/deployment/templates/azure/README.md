# Code Execution Server — Azure Container Apps Deployment

Infrastructure-as-code (Bicep) and helper scripts for deploying MCP code
execution servers to **Azure Container Apps (ACA)**.

## Prerequisites

| Tool | Purpose |
|------|---------|
| [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) | Resource provisioning and image push |
| [Bicep CLI](https://learn.microsoft.com/azure/azure-resource-manager/bicep/install) (ships with `az`) | Compile / deploy templates |
| Docker | Build container images |
| Python 3 + PyYAML (`yaml`) | `.env` parsing and `--network` manifest orchestration |

You also need:

* An Azure subscription with a resource group and a
  **user-assigned managed identity**.
* An **Entra ID (Azure AD) app registration** for the MCP server
  (`ENTRA_CLIENT_ID` / `ENTRA_TENANT_ID`).  The app must expose the
  `api://<client-id>/.default` scope and have a federated credential
  configured for the managed identity.

## Quick start

```bash
# 1. Authenticate
az login
az account set --subscription <SUBSCRIPTION_ID>

# 2. One-time setup (ACR, Log Analytics, ACA environment, role assignments)
./setup.sh \
  --resource-group  agora-mcp-rg \
  --location        eastus2 \
  --identity-id     /subscriptions/<SUB>/resourceGroups/<RG>/providers/Microsoft.ManagedIdentity/userAssignedIdentities/<NAME>

# 3. Copy the ACA_* values printed by setup.sh into `deployment/.env.server`.
#    The deploy scripts read infrastructure config (ACR, environment, identity) from
#    `deployment/.env.server` and pass it to Bicep — do NOT duplicate these in .bicepparam files.
#    See .env.server.example for the full list of ACA_* variables.

# 4. Set up Entra app registrations (MCP servers + Activity UI)
./setup-app-registrations.sh \
  --tenant-id <TENANT_ID> \
  --identity-id /subscriptions/<SUB>/resourceGroups/<RG>/providers/Microsoft.ManagedIdentity/userAssignedIdentities/<NAME>

# 5. Copy the printed ENTRA_* values into `deployment/.env.server`.
#    ACTIVITY_UI_* values configure the optional Activity UI deployment.

# 6. Deploy an example server (chemistry shown)
./deploy-server.sh --server chemistry

# 7. Or deploy a connector network (upstreams first, connectors in order)
./deploy-network.sh networks/science-hub.yaml

# 8. Verify
az containerapp show -n chemistry-server -g agora-mcp-rg --query properties.latestRevisionFqdn -o tsv
```

## Architecture

```
  One-time setup (setup.sh)              Per-deploy (deploy-server/network.sh + Bicep)
  ─────────────────────────              ──────────────────────────────
  ┌──────────────────────┐
  │   Resource Group     │
  ├──────────────────────┤
  │   Container Registry │◄──── docker push
  ├──────────────────────┤
  │   Managed Identity   │─── AcrPull role ──►  ACR
  ├──────────────────────┤
  │   Log Analytics      │
  ├──────────────────────┤
  │   ACA Managed Env    │
  └──────────┬───────────┘
             │
             │  ┌───────────────────────┐
             ├──│ activity-ui           │  ← Bicep (activity-ui.bicep)
             │  └───────────────────────┘
             │  ┌───────────────────────┐
             ├──│ chemistry-server app  │  ← Bicep (main.bicep)
             │  └───────────────────────┘
             │  ┌───────────────────────┐
             ├──│ earthscience-server   │
             │  └───────────────────────┘
             │  ┌───────────────────────┐
             └──│ energysystems-server  │
                └───────────────────────┘
```

## Files

| File | Description |
|------|-------------|
| `main.bicep` | Deploys a single MCP server Container App |
| `activity-ui.bicep` | Deploys the Activity UI monitoring sidecar (EasyAuth + managed identity) |
| `parameters/chemistry.bicepparam` | Parameter values for the chemistry example server |
| `parameters/earthscience.bicepparam` | Parameter values for the earth science example server |
| `parameters/energysystems.bicepparam` | Parameter values for the energy systems example server |
| `parameters/connector.bicepparam` | Connector parameter template (`CONNECTOR_MODE`, `UPSTREAM_*_URL`) |
| `parameters/activity-ui.bicepparam` | Parameter values for the Activity UI |
| `networks/science-hub.yaml` | Example network manifest for ordered upstream + connector deployment |
| `setup.sh` | One-time: creates ACR, Log Analytics, ACA environment, role assignments |
| `setup-app-registrations.sh` | One-time: creates Entra app registrations, app roles, identity grants |
| `deploy-server.sh` | Deploy a single server or Activity UI to ACA |
| `deploy-network.sh` | Deploy a connector network from a YAML manifest |
| `deploy.sh` | Dispatcher: routes `--network` to deploy-network.sh, else deploy-server.sh |
| `_deploy-common.sh` | Shared config loading and helper functions (sourced, not run directly) |

## Environment variables

The Container App receives these at runtime:

| Variable | Source | Description |
|----------|--------|-------------|
| `PORT` | hardcoded `8000` | HTTP listen port |
| `HOST` | hardcoded `0.0.0.0` | Bind address |
| `ENTRA_CLIENT_ID` | Bicep parameter | Entra app registration client ID |
| `ENTRA_TENANT_ID` | Bicep parameter | Entra tenant ID |
| `AZURE_CLIENT_ID` | Bicep parameter | Managed identity client ID |
| `OBO_SIMULATION_MODE` | hardcoded `false` | Must be false in production |

Connector-specific values are usually declared in `parameters/connector.bicepparam`, for example:

- `CONNECTOR_MODE` — `"router"` (default, aggregates multiple upstreams) or `"gateway"` (single upstream with policy enforcement)
- `UPSTREAM_<NAME>_URL` — one per upstream server (e.g., `UPSTREAM_CHEMISTRY_URL`)
- `GATEWAY_*` — optional gateway policy settings such as `GATEWAY_BLOCKED_TOOLS` and `GATEWAY_MAX_CALLS_PER_MINUTE`
- `OBJECT_TRANSFER_TRUSTED_HTTP_HOSTS` — set on **upstream** servers, not the connector
- `CONNECTOR_AUTH_FACTORY` — optional `"module.path:factory"` returning a custom `AuthConfig`, for
  deployments with their own identity provider. Takes precedence over `ENTRA_CLIENT_ID`/`ENTRA_TENANT_ID`.

Note: a connector with no auth backend configured (no `CONNECTOR_AUTH_FACTORY`, and
`ENTRA_CLIENT_ID`/`ENTRA_TENANT_ID` not both set) fails at startup rather than running
unauthenticated. `CONNECTOR_ALLOW_NOOP_AUTH=1` opts into disabled auth for local development only.

Note: gateway mode requires exactly one `UPSTREAM_*_URL`; multiple upstreams with
`CONNECTOR_MODE=gateway` is invalid and will fail at startup.

## Connector network deployment

`deploy-network.sh` deploys a full topology in order:

1. Deploy all upstream servers, health-checking each
2. Deploy connectors in dependency order, health-checking each

### Single-connector manifest

```yaml
name: science-hub
connector:
  server: science-hub
  params: ../parameters/connector.bicepparam
upstreams:
  - server: chemistry
    params: ../parameters/chemistry.bicepparam
    internal: true
  - server: earthscience
    params: ../parameters/earthscience.bicepparam
    internal: true
```

### Multi-connector manifest

Use `connectors` (plural) with `depends_on` to express ordering between connectors:

```yaml
name: science-hub-gateway
connectors:
  - server: science-router
    params: ../parameters/router.bicepparam
    internal: true
  - server: science-gateway
    params: ../parameters/gateway.bicepparam
    depends_on: [science-router]
upstreams:
  - server: chemistry
    params: ../parameters/chemistry.bicepparam
    internal: true
  - server: earthscience
    params: ../parameters/earthscience.bicepparam
    internal: true
```

This deploys: upstreams → science-router (health-check) → science-gateway.

### Manifest reference

| Field | Applies to | Description |
|-------|-----------|-------------|
| `server` | all | Container App name |
| `params` | all | Path to `.bicepparam` file |
| `internal` | all | If `true`, ingress is private (default: `true` for upstreams, `false` for connectors) |
| `dockerfile` | all | Path to Dockerfile (optional, auto-detected for upstreams) |
| `context` | all | Docker build context (optional, defaults to Dockerfile directory) |
| `port` | all | Health-check port (default: `8000`) |
| `depends_on` | connectors | List of connector `server` names that must deploy first |

### Behavior notes

- `internal: true` sets upstream ingress to `external: false` (internal-only).
- Connector deployments skip the Azure Files env-cache mount (stateless by default).
- Relative `params`, `dockerfile`, and `context` values are resolved from the manifest directory.
- Internal health checks use `curl` (or `wget`) inside the upstream container when ingress is private.
- `depends_on` is connector-to-connector only — upstreams are always deployed before any connector.
- Circular dependencies are detected and rejected at manifest parse time.

## Auth topology for connector networks

When a connector fronts domain servers, treat the connector as the primary external auth boundary.

Two supported patterns:

1. **One Entra app for the connector**  
   Upstream domain servers validate the connector managed-identity token for service-to-service calls.
2. **Shared audience / token pass-through**  
   Reuse existing end-user token validation if connector and upstreams share tenant/audience expectations.

## Activity UI deployment

The Activity UI is a lightweight monitoring sidecar that receives events from
MCP servers and streams them to browsers. Deploy it **before** MCP servers so
you can wire `ACTIVITY_UI_URL` into their environment.

### Prerequisites

1. An **Entra ID app registration** for the Activity UI (separate from MCP servers):
   - Redirect URI: `https://<activity-ui-fqdn>/.auth/login/aad/callback`
   - Application ID URI: `api://<client-id>`
   - Federated credential linking the managed identity (for secretless EasyAuth)
   - `ActivityEventWriter` app role assigned to the MCP managed identity
2. Use `setup-app-registrations.sh` to create all of the above automatically.

### Deploy

```bash
./deploy-server.sh \
  --server activity-ui \
  --template activity-ui.bicep \
  --dockerfile activity_ui/Dockerfile \
  --context . \
  --skip-base-build
```

No secrets are required — EasyAuth uses the managed identity's federated
credential for the OAuth code exchange.

### Wire MCP servers

After deployment, `deploy-server.sh` prints the Activity UI URL. Add these to your
`.env.server`:

```bash
# FQDN printed by deploy-server.sh
ACTIVITY_UI_URL=https://<activity-ui-fqdn>
# The Entra app registration client ID for the activity UI (audience for token acquisition)
ACTIVITY_UI_AUDIENCE=api://<activity-ui-entra-client-id>
```

Then redeploy MCP servers — the publisher acquires a managed-identity token
scoped to the activity UI's app registration and sends it as a Bearer token.
EasyAuth on the activity UI validates it automatically.

### How auth works

| Endpoint | Protection |
|----------|-----------|
| `/` `/stream` `/events/recent` | EasyAuth (Entra ID browser login via cookie) |
| `/events` | EasyAuth (Entra ID Bearer token from MCP server managed identity) |
| `/health` `/healthz` | Excluded from EasyAuth (ACA probes) |

No shared secrets are used. MCP servers authenticate using their managed
identity to acquire a token for the activity UI's app registration audience.

## Environment caching with Azure Files

For servers that build Python environments at startup or provision large assets
(model weights, data files), mount an Azure File Share so the cache persists
across container restarts and scale events.

### 1. Create a storage account and file share

```bash
az storage account create \
  --name agoramcpstorage \
  --resource-group agora-mcp-rg \
  --location eastus2 \
  --sku Standard_LRS

az storage share create \
  --name env-cache \
  --account-name agoramcpstorage
```

### 2. Link storage to the ACA environment

```bash
STORAGE_KEY=$(az storage account keys list \
  --account-name agoramcpstorage \
  --query '[0].value' -o tsv)

az containerapp env storage set \
  --name agora-mcp-envs \
  --resource-group agora-mcp-rg \
  --storage-name envcache \
  --azure-file-account-name agoramcpstorage \
  --azure-file-account-key "$STORAGE_KEY" \
  --azure-file-share-name env-cache \
  --access-mode ReadWrite
```

### 3. Deploy with the storage link

Add to your `.env.server`:

```bash
ACA_STORAGE_LINK=envcache
# ACA_CACHE_MOUNT_PATH=/home/appuser/.cache/mcp-envs  # default, override if needed
```

Then deploy as usual — `deploy-server.sh` passes the storage parameters to Bicep:

```bash
./deploy-server.sh --server my-server --dockerfile /path/to/Dockerfile --context /path/to/context
```

Or pass explicitly:

```bash
./deploy-server.sh --server my-server \
  --storage-link envcache \
  --dockerfile /path/to/Dockerfile --context /path/to/context
```

The first container replica will build the environment and provision assets into
the file share. Subsequent replicas (and restarts) reuse the cached content.

## Adding a new server

1. Copy one of `parameters/chemistry.bicepparam`, `parameters/earthscience.bicepparam`, or `parameters/energysystems.bicepparam` to `parameters/<name>.bicepparam`.
2. Update `serverName` and any server-specific overrides (e.g. cpu, memory).
3. Create a Dockerfile extending the base image with the warm-start pattern:
   ```dockerfile
   ARG BASE_IMAGE=mcp-server-base:local
   FROM ${BASE_IMAGE}

   COPY --chown=appuser:appuser path/to/your/server /app/servers/<name>

   # Pre-build environment during docker build (required for ACA deployment).
   # The server's --warm flag builds the conda/pip/uv environment and exits.
   # At runtime, the server detects the pre-built env and starts immediately.
   RUN python -m servers.<name>.server.<name>_server --warm

   CMD ["python", "-m", "servers.<name>.server.<name>_server"]
   ```
4. Ensure your server script handles `--warm`:
   ```python
   if __name__ == "__main__":
       if "--warm" in sys.argv:
           asyncio.run(server.warm())
       else:
           asyncio.run(server.run_http(host=host, port=port))
   ```
5. Run:
   ```bash
   ./deploy.sh --server <name>
   ```
