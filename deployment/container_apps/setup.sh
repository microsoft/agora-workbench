#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# One-time infrastructure setup for MCP code execution server deployment.
#
# Creates:
#   - Resource group (idempotent)
#   - Azure Container Registry (ACR)
#   - Log Analytics workspace
#   - Azure Container Apps managed environment
#   - AcrPull role assignment for the managed identity
#
# After running, copy the printed ACA_* values into deployment/.env.server.
#
# Prerequisites:
#   - az cli authenticated (`az login`)
#   - A pre-existing user-assigned managed identity
#
# Usage:
#   ./setup.sh \
#     --resource-group agora-mcp-rg \
#     --location eastus2 \
#     --identity-id /subscriptions/<SUB>/resourceGroups/<RG>/providers/Microsoft.ManagedIdentity/userAssignedIdentities/<NAME>
# ---------------------------------------------------------------------------

set -euo pipefail

# ── Defaults ─────────────────────────────────────────────────────────────────

RESOURCE_GROUP=""
LOCATION=""
IDENTITY_ID=""
ACR_NAME="agoramcpcr"
ENVIRONMENT_NAME="agora-mcp-envs"
WORKSPACE_NAME="agora-mcp-logs"

# ── Usage ────────────────────────────────────────────────────────────────────

usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Required:
  --resource-group, -g  NAME    Azure resource group name
  --location, -l        NAME    Azure region (e.g., eastus2)
  --identity-id         ID      Full resource ID of a user-assigned managed identity

Optional:
  --acr-name            NAME    Container registry name (default: agoramcpcr)
                                Must be globally unique across Azure.
  --environment-name    NAME    ACA managed environment name (default: agora-mcp-envs)
  --workspace-name      NAME    Log Analytics workspace name (default: agora-mcp-logs)
  -h, --help                    Show this help message
EOF
    exit 0
}

# ── Parse arguments ──────────────────────────────────────────────────────────

while [[ $# -gt 0 ]]; do
    case $1 in
        --resource-group|-g)  RESOURCE_GROUP="$2";    shift 2 ;;
        --location|-l)        LOCATION="$2";          shift 2 ;;
        --identity-id)        IDENTITY_ID="$2";       shift 2 ;;
        --acr-name)           ACR_NAME="$2";          shift 2 ;;
        --environment-name)   ENVIRONMENT_NAME="$2";  shift 2 ;;
        --workspace-name)     WORKSPACE_NAME="$2";    shift 2 ;;
        -h|--help)            usage ;;
        *)                    echo "Unknown option: $1" >&2; usage ;;
    esac
done

# ── Validate required arguments ──────────────────────────────────────────────

if [[ -z "$RESOURCE_GROUP" ]]; then
    echo "ERROR: --resource-group is required." >&2; exit 1
fi
if [[ -z "$LOCATION" ]]; then
    echo "ERROR: --location is required." >&2; exit 1
fi
if [[ -z "$IDENTITY_ID" ]]; then
    echo "ERROR: --identity-id is required." >&2; exit 1
fi

# ── Resolve identity details ─────────────────────────────────────────────────

echo ">> Resolving managed identity..."
IDENTITY_CLIENT_ID=$(az identity show --ids "$IDENTITY_ID" --query clientId -o tsv)
IDENTITY_PRINCIPAL_ID=$(az identity show --ids "$IDENTITY_ID" --query principalId -o tsv)
echo "   Client ID:    $IDENTITY_CLIENT_ID"
echo "   Principal ID: $IDENTITY_PRINCIPAL_ID"
echo ""

# ── 1. Resource Group ────────────────────────────────────────────────────────

echo ">> Creating resource group '$RESOURCE_GROUP' in '$LOCATION'..."
az group create \
    --name "$RESOURCE_GROUP" \
    --location "$LOCATION" \
    --output none
echo "   Done."
echo ""

# ── 2. Container Registry ───────────────────────────────────────────────────

echo ">> Creating Azure Container Registry '$ACR_NAME'..."
if ! az acr create \
    --name "$ACR_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --sku Basic \
    --output none 2>/dev/null; then
    echo "   ERROR: Failed to create ACR '$ACR_NAME'." >&2
    echo "   ACR names must be globally unique. Try a different name with --acr-name." >&2
    exit 1
fi
ACR_ID=$(az acr show --name "$ACR_NAME" --resource-group "$RESOURCE_GROUP" --query id -o tsv)
echo "   Done. ID: $ACR_ID"
echo ""

# ── 3. AcrPull role assignment ───────────────────────────────────────────────

echo ">> Assigning AcrPull role to managed identity..."
EXISTING=$(az role assignment list \
    --assignee "$IDENTITY_PRINCIPAL_ID" \
    --scope "$ACR_ID" \
    --role AcrPull \
    --query "length(@)" \
    -o tsv)

if [[ "$EXISTING" -gt 0 ]]; then
    echo "   Role assignment already exists, skipping."
else
    az role assignment create \
        --assignee-object-id "$IDENTITY_PRINCIPAL_ID" \
        --assignee-principal-type ServicePrincipal \
        --role AcrPull \
        --scope "$ACR_ID" \
        --output none
    echo "   Done."
fi
echo ""

# ── 4. Log Analytics workspace ───────────────────────────────────────────────

echo ">> Creating Log Analytics workspace '$WORKSPACE_NAME'..."
az monitor log-analytics workspace create \
    --resource-group "$RESOURCE_GROUP" \
    --workspace-name "$WORKSPACE_NAME" \
    --location "$LOCATION" \
    --output none 2>/dev/null || true  # Idempotent — ignores "already exists"

WORKSPACE_ID=$(az monitor log-analytics workspace show \
    --resource-group "$RESOURCE_GROUP" \
    --workspace-name "$WORKSPACE_NAME" \
    --query customerId -o tsv)
WORKSPACE_KEY=$(az monitor log-analytics workspace get-shared-keys \
    --resource-group "$RESOURCE_GROUP" \
    --workspace-name "$WORKSPACE_NAME" \
    --query primarySharedKey -o tsv)
echo "   Done. Workspace ID: $WORKSPACE_ID"
echo ""

# ── 5. Container Apps managed environment ────────────────────────────────────

echo ">> Creating ACA managed environment '$ENVIRONMENT_NAME'..."
az containerapp env create \
    --name "$ENVIRONMENT_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --location "$LOCATION" \
    --logs-workspace-id "$WORKSPACE_ID" \
    --logs-workspace-key "$WORKSPACE_KEY" \
    --output none 2>/dev/null || true  # Idempotent

echo "   Done."
echo ""

# ── Summary ──────────────────────────────────────────────────────────────────

ACR_LOGIN_SERVER=$(az acr show --name "$ACR_NAME" --resource-group "$RESOURCE_GROUP" --query loginServer -o tsv)

echo "============================================================"
echo " Setup complete!"
echo ""
echo " Add the following to deployment/.env.server:"
echo ""
echo "   ACA_RESOURCE_GROUP=$RESOURCE_GROUP"
echo "   ACA_ACR_NAME=$ACR_NAME"
echo "   ACA_ENVIRONMENT_NAME=$ENVIRONMENT_NAME"
echo "   ACA_IDENTITY_ID=$IDENTITY_ID"
echo "   ACA_IDENTITY_CLIENT_ID=$IDENTITY_CLIENT_ID"
echo ""
echo " ACR login server: $ACR_LOGIN_SERVER"
echo "============================================================"
