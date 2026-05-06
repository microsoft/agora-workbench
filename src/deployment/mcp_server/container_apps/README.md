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

# 4. Deploy the office server
./deploy.sh \
  --resource-group  agora-mcp-rg \
  --server          office

# 5. Verify
az containerapp show -n office-server -g agora-mcp-rg --query properties.latestRevisionFqdn -o tsv
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
             ├──│  office-server app    │  ← Bicep (main.bicep)
             │  └───────────────────────┘
             │  ┌───────────────────────┐
             └──│  (other servers)      │
                └───────────────────────┘
```

## Files

| File | Description |
|------|-------------|
| `main.bicep` | Deploys a single Container App into existing infrastructure |
| `parameters/office.bicepparam` | Parameter values for the office server |
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

## Adding a new server

1. Copy `parameters/office.bicepparam` to `parameters/<name>.bicepparam`.
2. Update `serverName` and any server-specific overrides (e.g. cpu, memory).
3. Optionally set the `command` parameter to override the image's `CMD`.
   When omitted, the container uses whatever `CMD` is set in your Dockerfile.
4. Run:
   ```bash
   ./deploy.sh --server <name> --dockerfile /path/to/Dockerfile --context /path/to/context
   ```
