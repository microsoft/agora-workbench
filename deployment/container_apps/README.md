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
#    deploy.sh reads infrastructure config (ACR, environment, identity) from
#    `deployment/.env.server` and passes it to Bicep — do NOT duplicate these in .bicepparam files.
#    See deploy.sh and .env.server.example for the full list of ACA_* variables.

# 4. Deploy an example server (chemistry shown)
./deploy.sh --server chemistry

# 5. Deploy a connector network (upstreams first, connector last)
./deploy.sh --network networks/science-hub.yaml

# 6. Verify
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
| `main.bicep` | Deploys a single Container App into existing infrastructure |
| `parameters/chemistry.bicepparam` | Parameter values for the chemistry example server |
| `parameters/earthscience.bicepparam` | Parameter values for the earth science example server |
| `parameters/energysystems.bicepparam` | Parameter values for the energy systems example server |
| `parameters/connector.bicepparam` | Connector parameter template (`MCP_CONNECTOR_*` + trusted hosts) |
| `networks/science-hub.yaml` | Example network manifest for ordered upstream + connector deployment |
| `setup.sh` | One-time: creates ACR, Log Analytics, ACA environment, role assignments |
| `deploy.sh` | Per-server and network orchestration with health gating |

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

- `CONNECTOR_MODE`
- `UPSTREAM_*` (one per upstream)
- `OBJECT_TRANSFER_TRUSTED_HTTP_HOSTS`

## Connector network deployment

`deploy.sh --network` deploys a full topology in order:

1. Deploy all upstream servers
2. Wait for each upstream `/health` endpoint to pass
3. Deploy the connector

Manifest format:

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

Behavior notes:

- `internal: true` sets upstream ingress to `external: false` (internal-only).
- Connector deployments skip the Azure Files env-cache mount (stateless by default).
- Relative `params`, `dockerfile`, and `context` values are resolved from the manifest directory.
- Internal health checks use `curl` (or `wget`) inside the upstream container when ingress is private.

## Auth topology for connector networks

When a connector fronts domain servers, treat the connector as the primary external auth boundary.

Two supported patterns:

1. **One Entra app for the connector**  
   Upstream domain servers validate the connector managed-identity token for service-to-service calls.
2. **Shared audience / token pass-through**  
   Reuse existing end-user token validation if connector and upstreams share tenant/audience expectations.

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
