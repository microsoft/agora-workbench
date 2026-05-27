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

# 3. Copy the ACA_* values printed by setup.sh into the repo-root .env file.
#    deploy.sh reads infrastructure config (ACR, environment, identity) from
#    .env and passes it to Bicep — do NOT duplicate these in .bicepparam files.
#    See deploy.sh and .env.example for the full list of ACA_* variables.

# 4. Deploy an example server (chemistry shown)
./deploy.sh \
  --resource-group  agora-mcp-rg \
  --server          chemistry

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

Add to your `.env`:

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
3. Optionally set the `command` parameter to override the image's `CMD`.
   When omitted, the container uses whatever `CMD` is set in your Dockerfile.
4. Run:
   ```bash
   ./deploy.sh --server <name> --dockerfile /path/to/Dockerfile --context /path/to/context
   ```
