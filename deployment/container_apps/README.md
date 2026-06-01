# Code Execution Server — Azure Container Apps Deployment

Infrastructure-as-code (Bicep) and helper scripts for deploying MCP code
execution servers to **Azure Container Apps (ACA)**.

## Prerequisites

| Tool | Purpose |
|------|---------|
| [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) | Resource provisioning and image push |
| [Bicep CLI](https://learn.microsoft.com/azure/azure-resource-manager/bicep/install) (ships with `az`) | Compile / deploy templates |
| Docker | Build container images |

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
#    deploy.sh reads infrastructure config (ACR, environment, identity) from
#    `deployment/.env.server` and passes it to Bicep — do NOT duplicate these in .bicepparam files.
#    See deploy.sh and .env.server.example for the full list of ACA_* variables.

# 4. Deploy an example server (chemistry shown)
./deploy.sh --server chemistry

# 5. Verify
az containerapp show -n chemistry-server -g agora-mcp-rg --query properties.latestRevisionFqdn -o tsv
```

## Architecture

```
  One-time setup (setup.sh)              Per-server (deploy.sh + Bicep)
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
| `activity-ui.bicep` | Deploys the Activity UI monitoring sidecar (EasyAuth + ingest secret) |
| `parameters/chemistry.bicepparam` | Parameter values for the chemistry example server |
| `parameters/earthscience.bicepparam` | Parameter values for the earth science example server |
| `parameters/energysystems.bicepparam` | Parameter values for the energy systems example server |
| `parameters/activity-ui.bicepparam` | Parameter values for the Activity UI |
| `setup.sh` | One-time: creates ACR, Log Analytics, ACA environment, role assignments |
| `deploy.sh` | Per-server: builds image, pushes to ACR, deploys Container App |

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

## Activity UI deployment

The Activity UI is a lightweight monitoring sidecar that receives events from
MCP servers and streams them to browsers. Deploy it **before** MCP servers so
you can wire `ACTIVITY_UI_URL` into their environment.

### Prerequisites

1. An **Entra ID app registration** for the Activity UI (separate from MCP servers):
   - Redirect URI: `https://<activity-ui-fqdn>/.auth/login/aad/callback`
   - Generate a client secret (for EasyAuth browser login)
   - Expose an API scope: `api://<client-id>/.default`
2. Grant the MCP servers' **managed identity** permission to acquire tokens for
   the activity UI's app registration (add it as an authorized client application,
   or assign an app role).

### Deploy

```bash
./deploy.sh \
  --server activity-ui \
  --template activity-ui.bicep \
  --dockerfile activity_ui/Dockerfile \
  --context . \
  --skip-base-build
```

The `entraClientSecret` parameter is required by the Bicep template. Pass it
at deploy time via the az CLI prompt, or supply it directly:

```bash
az deployment group create ... --parameters entraClientSecret="<secret>"
```

### Wire MCP servers

After deployment, `deploy.sh` prints the Activity UI URL. Add these to your
`.env.server`:

```bash
# FQDN printed by deploy.sh
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

Then deploy as usual — `deploy.sh` passes the storage parameters to Bicep:

```bash
./deploy.sh --server my-server --dockerfile /path/to/Dockerfile --context /path/to/context
```

Or pass explicitly:

```bash
./deploy.sh --server my-server \
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

   COPY --chown=appuser:appuser path/to/your/server /app/domain_examples/<name>

   # Pre-build environment during docker build (required for ACA deployment).
   # The server's --warm flag builds the conda/pip/uv environment and exits.
   # At runtime, the server detects the pre-built env and starts immediately.
   RUN python -m domain_examples.<name>.server.<name>_server --warm

   CMD ["python", "-m", "domain_examples.<name>.server.<name>_server"]
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
