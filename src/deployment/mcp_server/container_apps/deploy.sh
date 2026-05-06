#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Deploy an MCP code execution server to Azure Container Apps.
#
# This script:
#   1. Loads infrastructure config from .env
#   2. Builds the Docker image for the requested server
#   3. Pushes the image to the existing Azure Container Registry
#   4. Deploys (or updates) the Container App via Bicep
#
# Infrastructure values (resource group, ACR, environment, identity) are read
# from ACA_* variables in .env.  Server-specific sizing lives in the
# parameters/<name>.bicepparam file.
#
# Prerequisites:
#   - az cli authenticated (`az login`)
#   - Docker daemon running
#   - .env configured with ACA_* variables (see .env.example)
#
# Usage:
#   ./deploy.sh --server office
#   ./deploy.sh --server office --tag v1.2.3
# ---------------------------------------------------------------------------

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# ── Load .env ────────────────────────────────────────────────────────────────

ENV_FILE="${REPO_ROOT}/.env"
if [[ -f "$ENV_FILE" ]]; then
    # Source only non-comment, non-empty lines; strip surrounding quotes
    set -a
    while IFS='=' read -r key value; do
        # Skip comments and blank lines
        [[ "$key" =~ ^[[:space:]]*# ]] && continue
        [[ -z "$key" ]] && continue
        # Strip leading/trailing whitespace and optional quotes from value
        value="${value#\"}"; value="${value%\"}"
        value="${value#\'}"; value="${value%\'}"
        export "$key=$value"
    done < <(grep -v '^[[:space:]]*$' "$ENV_FILE")
    set +a
else
    echo "WARNING: .env not found at $ENV_FILE — using environment variables only." >&2
fi

# ── Defaults from .env (ACA_* variables) ─────────────────────────────────────

RESOURCE_GROUP="${ACA_RESOURCE_GROUP:-}"
ACR_NAME="${ACA_ACR_NAME:-agoramcpcr}"
ACA_ENV_NAME="${ACA_ENVIRONMENT_NAME:-agora-mcp-envs}"
IDENTITY_ID="${ACA_IDENTITY_ID:-}"
IDENTITY_CLIENT_ID="${ACA_IDENTITY_CLIENT_ID:-}"
ENTRA_CLIENT_ID_VAL="${ENTRA_CLIENT_ID:-}"
ENTRA_TENANT_ID_VAL="${ENTRA_TENANT_ID:-}"

SERVER_NAME=""
IMAGE_TAG=""                         # auto-set to git short SHA if empty
PARAM_FILE=""                        # auto-resolved from server name

usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Required:
  --server, -s          NAME    Server name (must match a Dockerfile target and
                                a parameters/<name>.bicepparam file)

Optional:
  --resource-group, -g  NAME    Override ACA_RESOURCE_GROUP from .env
  --tag, -t             TAG     Docker image tag (default: git short SHA)
  --acr-name            NAME    Override ACA_ACR_NAME from .env
  --param-file          PATH    Bicep parameter file override
  -h, --help                    Show this help message
EOF
    exit 0
}

# ── Parse arguments ──────────────────────────────────────────────────────────

while [[ $# -gt 0 ]]; do
    case $1 in
        --resource-group|-g)  RESOURCE_GROUP="$2";  shift 2 ;;
        --server|-s)          SERVER_NAME="$2";     shift 2 ;;
        --tag|-t)             IMAGE_TAG="$2";       shift 2 ;;
        --acr-name)           ACR_NAME="$2";        shift 2 ;;
        --param-file)         PARAM_FILE="$2";      shift 2 ;;
        -h|--help)            usage ;;
        *)                    echo "Unknown option: $1"; usage ;;
    esac
done

if [[ -z "$SERVER_NAME" ]]; then
    echo "ERROR: --server is required." >&2; exit 1
fi

# Validate required infrastructure variables
for var in RESOURCE_GROUP ACR_NAME ACA_ENV_NAME IDENTITY_ID IDENTITY_CLIENT_ID ENTRA_CLIENT_ID_VAL ENTRA_TENANT_ID_VAL; do
    if [[ -z "${!var}" ]]; then
        echo "ERROR: $var is not set. Configure ACA_* variables in .env or pass as flags." >&2
        exit 1
    fi
done

if [[ -z "$IMAGE_TAG" ]]; then
    IMAGE_TAG="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo 'latest')"
fi

if [[ -z "$PARAM_FILE" ]]; then
    PARAM_FILE="$SCRIPT_DIR/parameters/${SERVER_NAME}.bicepparam"
fi

if [[ ! -f "$PARAM_FILE" ]]; then
    echo "ERROR: Parameter file not found: $PARAM_FILE" >&2
    echo "Create it in deploy/parameters/${SERVER_NAME}.bicepparam" >&2
    exit 1
fi

DOCKER_TARGET="${SERVER_NAME}-server"
DOCKERFILE="$REPO_ROOT/deployment/mcp_server/Dockerfile"
ACR_LOGIN_SERVER="${ACR_NAME}.azurecr.io"
IMAGE_REF="${ACR_LOGIN_SERVER}/${SERVER_NAME}-server:${IMAGE_TAG}"

# Resolve the environment resource ID
ENV_ID=$(az containerapp env show \
    --resource-group "$RESOURCE_GROUP" \
    --name "$ACA_ENV_NAME" \
    --query id --output tsv)

echo "=== MCP Server ACA Deployment ==="
echo "  Server:         $SERVER_NAME"
echo "  Resource Group: $RESOURCE_GROUP"
echo "  ACR:            $ACR_LOGIN_SERVER"
echo "  ACA Env:        $ACA_ENV_NAME"
echo "  Image:          $IMAGE_REF"
echo "  Param file:     $PARAM_FILE"
echo ""

# ── 1. Build Docker image ────────────────────────────────────────────────────

echo ">> Building Docker image (target: $DOCKER_TARGET)..."
docker build \
    --file "$DOCKERFILE" \
    --target "$DOCKER_TARGET" \
    --tag "$IMAGE_REF" \
    "$REPO_ROOT"

# ── 2. Push to ACR ───────────────────────────────────────────────────────────

echo ">> Logging in to ACR..."
az acr login --name "$ACR_NAME"

echo ">> Pushing image to ACR..."
docker push "$IMAGE_REF"

# ── 3. Build extraEnvVars from .env ───────────────────────────────────────────
# Forward all .env variables to the Container App, excluding:
#   - ACA_*             (deployment infrastructure only)
#   - ENTRA_*           (already in baseEnv via dedicated params)
#   - OBO_SIMULATION_MODE (hardcoded to false in baseEnv)
#   - Variables with empty values

EXTRA_ENV_JSON="{"
first=true
if [[ -f "$ENV_FILE" ]]; then
    while IFS='=' read -r key value; do
        [[ "$key" =~ ^[[:space:]]*# ]] && continue
        [[ -z "$key" ]] && continue
        # Strip quotes
        value="${value#\"}"; value="${value%\"}"
        value="${value#\'}"; value="${value%\'}"
        [[ -z "$value" ]] && continue
        # Skip infra / already-handled vars
        [[ "$key" =~ ^ACA_ ]] && continue
        [[ "$key" =~ ^ENTRA_ ]] && continue
        [[ "$key" == "OBO_SIMULATION_MODE" ]] && continue
        if [[ "$first" == true ]]; then
            first=false
        else
            EXTRA_ENV_JSON+=","
        fi
        # Escape any double quotes in value
        escaped_value="${value//\\/\\\\}"
        escaped_value="${escaped_value//\"/\\\"}"
        EXTRA_ENV_JSON+="\"$key\":\"$escaped_value\""
    done < <(grep -v '^[[:space:]]*$' "$ENV_FILE")
fi
EXTRA_ENV_JSON+="}"

echo "  Extra env vars:  $(echo "$EXTRA_ENV_JSON" | python3 -c "import sys,json; print(', '.join(json.loads(sys.stdin.read()).keys()))")"
echo ""

# ── 4. Deploy Bicep ──────────────────────────────────────────────────────────

echo ">> Deploying Container App via Bicep..."
az deployment group create \
    --resource-group "$RESOURCE_GROUP" \
    --template-file "$SCRIPT_DIR/main.bicep" \
    --parameters "$PARAM_FILE" \
    --parameters \
        containerImage="$IMAGE_REF" \
        environmentId="$ENV_ID" \
        identityId="$IDENTITY_ID" \
        identityClientId="$IDENTITY_CLIENT_ID" \
        registryServer="$ACR_LOGIN_SERVER" \
        entraClientId="$ENTRA_CLIENT_ID_VAL" \
        entraTenantId="$ENTRA_TENANT_ID_VAL" \
        extraEnvVars="$EXTRA_ENV_JSON" \
    --output none

# ── 5. Report ────────────────────────────────────────────────────────────────

FQDN=$(az containerapp show \
    --name "${SERVER_NAME}-server" \
    --resource-group "$RESOURCE_GROUP" \
    --query 'properties.configuration.ingress.fqdn' \
    --output tsv)

echo ""
echo "=== Deployment complete ==="
echo "  App URL:  https://${FQDN}"
echo "  Health:   https://${FQDN}/health"
