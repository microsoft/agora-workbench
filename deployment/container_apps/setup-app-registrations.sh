#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Set up Entra ID app registrations for MCP server and Activity UI deployment.
#
# Creates (idempotently):
#   - MCP Servers app registration (token audience for client validation)
#   - Activity UI app registration (EasyAuth + service-to-service audience)
#   - Federated credential on Activity UI app (secretless EasyAuth via managed identity)
#   - App role on Activity UI for managed identity (service-to-service auth)
#   - App role assignment granting the managed identity access to the Activity UI
#
# No secrets are created or required — the entire auth chain is credential-based
# using managed identity and workload identity federation.
#
# After running, copy the printed values into deployment/.env.server.
#
# Prerequisites:
#   - az cli authenticated (`az login`) with permissions to create app registrations
#   - A pre-existing user-assigned managed identity
#
# Usage:
#   ./setup-app-registrations.sh \
#     --tenant-id 72f988bf-86f1-41af-91ab-2d7cd011db47 \
#     --resource-group agora-mcp-rg \
#     --identity-id /subscriptions/<SUB>/resourceGroups/<RG>/providers/Microsoft.ManagedIdentity/userAssignedIdentities/<NAME>
#
#   # With Activity UI FQDN (if already deployed):
#   ./setup-app-registrations.sh \
#     --tenant-id <TENANT> \
#     --resource-group <RG> \
#     --identity-id <ID> \
#     --activity-ui-fqdn activity-ui.happyocean.eastus.azurecontainerapps.io
# ---------------------------------------------------------------------------

set -euo pipefail

# ── Defaults ─────────────────────────────────────────────────────────────────

TENANT_ID=""
IDENTITY_ID=""
RESOURCE_GROUP=""
MCP_APP_NAME=""
ACTIVITY_UI_APP_NAME=""
ACTIVITY_UI_FQDN=""

# ── Usage ────────────────────────────────────────────────────────────────────

usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Required:
  --tenant-id, -t       ID      Entra ID (Azure AD) tenant ID
  --identity-id         ID      Full resource ID of the MCP servers' managed identity
  --resource-group, -g  NAME    Resource group name (scopes app identifier URIs for uniqueness)

Optional:
  --mcp-app-name        NAME    Display name for MCP app registration (default: "Agora MCP Servers")
  --activity-ui-name    NAME    Display name for Activity UI app registration (default: "Agora Activity UI")
  --activity-ui-fqdn    FQDN    Activity UI hostname (sets redirect URI for EasyAuth callback)
  -h, --help                    Show this help message
EOF
    exit 0
}

# ── Parse arguments ──────────────────────────────────────────────────────────

while [[ $# -gt 0 ]]; do
    case $1 in
        --tenant-id|-t)       TENANT_ID="$2";          shift 2 ;;
        --identity-id)        IDENTITY_ID="$2";        shift 2 ;;
        --resource-group|-g)  RESOURCE_GROUP="$2";     shift 2 ;;
        --mcp-app-name)       MCP_APP_NAME="$2";       shift 2 ;;
        --activity-ui-name)   ACTIVITY_UI_APP_NAME="$2"; shift 2 ;;
        --activity-ui-fqdn)   ACTIVITY_UI_FQDN="$2";   shift 2 ;;
        -h|--help)            usage ;;
        *)                    echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

# ── Validate ─────────────────────────────────────────────────────────────────

if [[ -z "$TENANT_ID" ]]; then
    echo "ERROR: --tenant-id is required." >&2; exit 1
fi
if [[ -z "$IDENTITY_ID" ]]; then
    echo "ERROR: --identity-id is required." >&2; exit 1
fi
if [[ -z "$RESOURCE_GROUP" ]]; then
    echo "ERROR: --resource-group is required." >&2; exit 1
fi

# Default display names (clean, human-friendly).
if [[ -z "$MCP_APP_NAME" ]]; then
    MCP_APP_NAME="Agora MCP Servers"
fi
if [[ -z "$ACTIVITY_UI_APP_NAME" ]]; then
    ACTIVITY_UI_APP_NAME="Agora Activity UI"
fi

# Identifier URIs are scoped by resource group for uniqueness within the tenant.
# These serve as the canonical lookup key (display names are not unique in Entra).
MCP_IDENTIFIER_URI="api://agora-mcp-servers.${RESOURCE_GROUP}"
UI_IDENTIFIER_URI="api://agora-activity-ui.${RESOURCE_GROUP}"

# ── Helpers ──────────────────────────────────────────────────────────────────

# Find an app registration by its identifier URI. Returns appId or empty string.
find_app_by_uri() {
    local uri="$1"
    az ad app show --id "$uri" --query appId -o tsv 2>/dev/null || true
}

# Generate a stable GUID from a seed string (deterministic, avoids drift on re-runs).
generate_uuid() {
    python3 -c "import uuid, sys; print(uuid.uuid5(uuid.NAMESPACE_DNS, sys.argv[1]))" "$1"
}

echo "=== Entra ID App Registration Setup ==="
echo "  Tenant:         $TENANT_ID"
echo "  Resource group: $RESOURCE_GROUP"
echo "  MCP app:        $MCP_APP_NAME  ($MCP_IDENTIFIER_URI)"
echo "  Activity UI:    $ACTIVITY_UI_APP_NAME  ($UI_IDENTIFIER_URI)"
echo ""

# ── 1. Resolve managed identity ─────────────────────────────────────────────

echo ">> Resolving managed identity..."
MI_CLIENT_ID=$(az identity show --ids "$IDENTITY_ID" --query clientId -o tsv)
MI_OBJECT_ID=$(az ad sp show --id "$MI_CLIENT_ID" --query id -o tsv 2>/dev/null || true)

if [[ -z "$MI_OBJECT_ID" ]]; then
    echo "   Creating service principal for managed identity..."
    MI_OBJECT_ID=$(az ad sp create --id "$MI_CLIENT_ID" --query id -o tsv)
fi
echo "   Client ID:  $MI_CLIENT_ID"
echo "   Object ID:  $MI_OBJECT_ID"
echo ""

# ── 2. MCP Servers app registration ─────────────────────────────────────────

echo ">> Setting up MCP Servers app registration..."
MCP_APP_ID=$(find_app_by_uri "$MCP_IDENTIFIER_URI")

if [[ -n "$MCP_APP_ID" ]]; then
    echo "   Found existing: $MCP_APP_ID"
else
    echo "   Creating app registration '$MCP_APP_NAME'..."
    MCP_APP_ID=$(az ad app create \
        --display-name "$MCP_APP_NAME" \
        --sign-in-audience AzureADMyOrg \
        --query appId -o tsv)
    echo "   Created: $MCP_APP_ID"

    # Ensure service principal exists
    az ad sp create --id "$MCP_APP_ID" --output none 2>/dev/null || true
fi

# Set Application ID URI (resource-group-scoped for tenant uniqueness)
echo "   Setting identifier URI ($MCP_IDENTIFIER_URI)..."
az ad app update --id "$MCP_APP_ID" \
    --identifier-uris "$MCP_IDENTIFIER_URI" \
    --output none
echo "   Done."
echo ""

# ── 3. Activity UI app registration ─────────────────────────────────────────

echo ">> Setting up Activity UI app registration..."
UI_APP_ID=$(find_app_by_uri "$UI_IDENTIFIER_URI")

if [[ -n "$UI_APP_ID" ]]; then
    echo "   Found existing: $UI_APP_ID"
else
    # Set redirect URI if FQDN is known, otherwise use placeholder
    if [[ -n "$ACTIVITY_UI_FQDN" ]]; then
        REDIRECT_URI="https://${ACTIVITY_UI_FQDN}/.auth/login/aad/callback"
    else
        REDIRECT_URI="https://localhost/.auth/login/aad/callback"
    fi

    echo "   Creating app registration '$ACTIVITY_UI_APP_NAME'..."
    UI_APP_ID=$(az ad app create \
        --display-name "$ACTIVITY_UI_APP_NAME" \
        --sign-in-audience AzureADMyOrg \
        --web-redirect-uris "$REDIRECT_URI" \
        --query appId -o tsv)
    echo "   Created: $UI_APP_ID"

    # Ensure service principal exists
    az ad sp create --id "$UI_APP_ID" --output none 2>/dev/null || true
fi

# Set Application ID URI (resource-group-scoped for tenant uniqueness)
echo "   Setting identifier URI ($UI_IDENTIFIER_URI)..."
az ad app update --id "$UI_APP_ID" \
    --identifier-uris "api://$UI_APP_ID" "$UI_IDENTIFIER_URI" \
    --output none

# Update redirect URI if FQDN provided (appends to existing URIs)
if [[ -n "$ACTIVITY_UI_FQDN" ]]; then
    NEW_URI="https://${ACTIVITY_UI_FQDN}/.auth/login/aad/callback"
    EXISTING_URIS=$(az ad app show --id "$UI_APP_ID" \
        --query "web.redirectUris" -o tsv 2>/dev/null || true)
    if echo "$EXISTING_URIS" | grep -qF "$NEW_URI"; then
        echo "   Redirect URI already configured."
    else
        # Collect existing + new into a space-separated list
        ALL_URIS=$(echo "$EXISTING_URIS" | tr '\n' ' ')
        ALL_URIS="${ALL_URIS}${NEW_URI}"
        echo "   Adding redirect URI for FQDN: $ACTIVITY_UI_FQDN"
        az ad app update --id "$UI_APP_ID" \
            --web-redirect-uris $ALL_URIS \
            --output none
    fi
fi

echo "   Done."
echo ""

# ── 4. Define app role on Activity UI for service-to-service access ──────────

echo ">> Configuring app role on Activity UI..."
APP_ROLE_ID=$(generate_uuid "ActivityEventWriter-${UI_APP_ID}")

# Read current app roles and check if ours exists
EXISTING_ROLE=$(az ad app show --id "$UI_APP_ID" \
    --query "appRoles[?value=='ActivityEventWriter'].id" -o tsv)

if [[ -n "$EXISTING_ROLE" ]]; then
    echo "   App role 'ActivityEventWriter' already exists."
    APP_ROLE_ID="$EXISTING_ROLE"
else
    echo "   Adding app role 'ActivityEventWriter'..."
    # Merge with existing app roles to avoid overwriting them
    UI_OBJECT_ID=$(az ad app show --id "$UI_APP_ID" --query id -o tsv)
    EXISTING_ROLES=$(az rest --method GET \
        --uri "https://graph.microsoft.com/v1.0/applications/${UI_OBJECT_ID}" \
        --query "appRoles" 2>/dev/null || echo "[]")
    NEW_ROLE="{\"id\": \"$APP_ROLE_ID\", \"allowedMemberTypes\": [\"Application\"], \"displayName\": \"Activity Event Writer\", \"description\": \"Allows MCP servers to publish events to the Activity UI\", \"value\": \"ActivityEventWriter\", \"isEnabled\": true}"
    MERGED_ROLES=$(python3 -c "
import json, sys
existing = json.loads(sys.argv[1]) if sys.argv[1] != '[]' else []
existing.append(json.loads(sys.argv[2]))
print(json.dumps({'appRoles': existing}))
" "$EXISTING_ROLES" "$NEW_ROLE")
    az rest --method PATCH \
        --uri "https://graph.microsoft.com/v1.0/applications/${UI_OBJECT_ID}" \
        --headers "Content-Type=application/json" \
        --body "$MERGED_ROLES" --output none
    echo "   Done."
fi
echo ""

# ── 5. Assign app role to managed identity ───────────────────────────────────

echo ">> Assigning app role to managed identity..."
UI_SP_ID=$(az ad sp show --id "$UI_APP_ID" --query id -o tsv)

# Check if assignment already exists
EXISTING_ASSIGNMENT=$(az rest --method GET \
    --uri "https://graph.microsoft.com/v1.0/servicePrincipals/${UI_SP_ID}/appRoleAssignedTo" \
    --query "value[?principalId=='${MI_OBJECT_ID}' && appRoleId=='${APP_ROLE_ID}'].id" -o tsv 2>/dev/null || true)

if [[ -n "$EXISTING_ASSIGNMENT" ]]; then
    echo "   Role assignment already exists."
else
    az rest --method POST \
        --uri "https://graph.microsoft.com/v1.0/servicePrincipals/${UI_SP_ID}/appRoleAssignedTo" \
        --headers "Content-Type=application/json" \
        --body "{
            \"principalId\": \"$MI_OBJECT_ID\",
            \"resourceId\": \"$UI_SP_ID\",
            \"appRoleId\": \"$APP_ROLE_ID\"
        }" --output none
    echo "   Done."
fi
echo ""

# ── 6. Add federated credential for EasyAuth (secretless browser login) ──────
# This allows ACA EasyAuth to authenticate as the app registration using the
# managed identity — no client secret needed for the OAuth code exchange.

echo ">> Configuring federated credential for EasyAuth..."
EXISTING_FED_CRED=$(az ad app federated-credential list --id "$UI_APP_ID" \
    --query "[?name=='aca-easyauth'].name" -o tsv 2>/dev/null || true)

if [[ -n "$EXISTING_FED_CRED" ]]; then
    echo "   Federated credential 'aca-easyauth' already exists."
else
    echo "   Adding federated credential (managed identity → EasyAuth)..."
    az ad app federated-credential create --id "$UI_APP_ID" \
        --parameters "{
            \"name\": \"aca-easyauth\",
            \"issuer\": \"https://login.microsoftonline.com/${TENANT_ID}/v2.0\",
            \"subject\": \"${MI_CLIENT_ID}\",
            \"audiences\": [\"api://AzureADTokenExchange\"]
        }" --output none
    echo "   Done."
fi
echo ""

# ── Summary ──────────────────────────────────────────────────────────────────

echo "============================================================"
echo " App registration setup complete! (fully secretless)"
echo ""
echo " Add the following to deployment/.env.server:"
echo ""
echo "   # MCP Server auth (token validation)"
echo "   ENTRA_CLIENT_ID=$MCP_APP_ID"
echo "   ENTRA_TENANT_ID=$TENANT_ID"
echo ""
echo "   # Activity UI (publisher token acquisition)"
echo "   ACTIVITY_UI_AUDIENCE=$UI_IDENTIFIER_URI"
echo ""
echo " For Activity UI Bicep deployment:"
echo ""
echo "   entraClientId=$UI_APP_ID"
echo "   entraTenantId=$TENANT_ID"
echo "   identityId=<your-managed-identity-resource-id>"
echo ""
echo " EasyAuth uses the managed identity's federated credential"
echo " for the OAuth code exchange — no client secret required."
echo ""
if [[ -z "$ACTIVITY_UI_FQDN" ]]; then
    echo " NOTE: After deploying the Activity UI, update the redirect URI:"
    echo "   az ad app update --id $UI_APP_ID \\"
    echo "     --web-redirect-uris \"https://<fqdn>/.auth/login/aad/callback\""
    echo ""
fi
echo "============================================================"
