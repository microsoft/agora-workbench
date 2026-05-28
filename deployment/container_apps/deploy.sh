#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Deploy an MCP code execution server to Azure Container Apps.
#
# This script:
#   1. Loads infrastructure config from .env.server
#   2. Builds the Docker image for the requested server
#   3. Pushes the image to the existing Azure Container Registry
#   4. Deploys (or updates) the Container App via Bicep
#
# Infrastructure values (resource group, ACR, environment, identity) are read
# from ACA_* variables in .env.server.  Server-specific sizing lives in the
# parameters/<name>.bicepparam file.
#
# Prerequisites:
#   - az cli authenticated (`az login`)
#   - Docker daemon running
#   - .env.server configured with ACA_* variables (see .env.server.example)
#
# Usage:
#   ./deploy.sh --server chemistry --dockerfile path/to/Dockerfile --context path/to/context
#   ./deploy.sh --server chemistry --dockerfile path/to/Dockerfile --context path/to/context --tag v1.2.3
# ---------------------------------------------------------------------------

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# ── Load .env.server ──────────────────────────────────────────────────────────

ENV_FILE="${SCRIPT_DIR}/../.env.server"
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
    echo "WARNING: .env.server not found at $ENV_FILE — using environment variables only." >&2
fi

# ── Defaults from .env.server (ACA_* variables) ─────────────────────────────────────

RESOURCE_GROUP="${ACA_RESOURCE_GROUP:-}"
ACR_NAME="${ACA_ACR_NAME:-agoramcpcr}"
ACA_ENV_NAME="${ACA_ENVIRONMENT_NAME:-agora-mcp-envs}"
IDENTITY_ID="${ACA_IDENTITY_ID:-}"
IDENTITY_CLIENT_ID="${ACA_IDENTITY_CLIENT_ID:-}"
ENTRA_CLIENT_ID_VAL="${ENTRA_CLIENT_ID:-}"
ENTRA_TENANT_ID_VAL="${ENTRA_TENANT_ID:-}"
STORAGE_LINK="${ACA_STORAGE_LINK:-}"
CACHE_MOUNT_PATH="${ACA_CACHE_MOUNT_PATH:-/home/appuser/.cache/mcp-envs}"

SERVER_NAME=""
IMAGE_TAG=""                         # auto-set to git short SHA if empty
PARAM_FILE=""                        # auto-resolved from server name
DOCKERFILE=""                        # defaults to deployment/base.Dockerfile
BUILD_CONTEXT=""                     # defaults to repo root
DRY_RUN=false

usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Required:
  --server, -s          NAME    Server name (used for image naming and
                                parameters/<name>.bicepparam lookup)

Optional:
  --dockerfile, -f      PATH    Path to the Dockerfile to build
                                (default: deployment/base.Dockerfile)
  --context, -c         PATH    Docker build context directory
                                (default: repository root)

Optional:
  --resource-group, -g  NAME    Override ACA_RESOURCE_GROUP from .env.server
  --tag, -t             TAG     Docker image tag (default: git short SHA)
  --acr-name            NAME    Override ACA_ACR_NAME from .env.server
  --storage-link        NAME    ACA environment storage link name for Azure Files cache
  --cache-mount-path    PATH    Container mount path for cache (default: /home/appuser/.cache/mcp-envs)
  --param-file          PATH    Bicep parameter file override
  --dry-run                     Show what would be deployed without building or deploying
  -h, --help                    Show this help message
EOF
    exit 0
}

# ── Parse arguments ──────────────────────────────────────────────────────────

while [[ $# -gt 0 ]]; do
    case $1 in
        --resource-group|-g)  RESOURCE_GROUP="$2";  shift 2 ;;
        --server|-s)          SERVER_NAME="$2";     shift 2 ;;
        --dockerfile|-f)      DOCKERFILE="$2";      shift 2 ;;
        --context|-c)         BUILD_CONTEXT="$2";   shift 2 ;;
        --tag|-t)             IMAGE_TAG="$2";       shift 2 ;;
        --acr-name)           ACR_NAME="$2";        shift 2 ;;
        --storage-link)       STORAGE_LINK="$2";    shift 2 ;;
        --cache-mount-path)   CACHE_MOUNT_PATH="$2"; shift 2 ;;
        --param-file)         PARAM_FILE="$2";      shift 2 ;;
        --dry-run)            DRY_RUN=true;         shift ;;
        -h|--help)            usage ;;
        *)                    echo "Unknown option: $1"; usage ;;
    esac
done

if [[ -z "$SERVER_NAME" ]]; then
    echo "ERROR: --server is required." >&2; exit 1
fi

if [[ -z "$DOCKERFILE" ]]; then
    # Prefer a server-specific Dockerfile if one exists
    SERVER_DOCKERFILE="${REPO_ROOT}/examples/domain_examples/${SERVER_NAME}/Dockerfile"
    if [[ -f "$SERVER_DOCKERFILE" ]]; then
        DOCKERFILE="$SERVER_DOCKERFILE"
    else
        DOCKERFILE="${REPO_ROOT}/deployment/base.Dockerfile"
    fi
fi

if [[ ! -f "$DOCKERFILE" && "$DRY_RUN" == false ]]; then
    echo "ERROR: Dockerfile not found: $DOCKERFILE" >&2; exit 1
fi

if [[ -z "$BUILD_CONTEXT" ]]; then
    BUILD_CONTEXT="${REPO_ROOT}"
fi

if [[ ! -d "$BUILD_CONTEXT" && "$DRY_RUN" == false ]]; then
    echo "ERROR: Build context directory not found: $BUILD_CONTEXT" >&2; exit 1
fi

# Validate required infrastructure variables
for var in RESOURCE_GROUP ACR_NAME ACA_ENV_NAME IDENTITY_ID IDENTITY_CLIENT_ID ENTRA_CLIENT_ID_VAL ENTRA_TENANT_ID_VAL; do
    if [[ -z "${!var}" ]]; then
        echo "ERROR: $var is not set. Configure ACA_* variables in .env.server or pass as flags." >&2
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

DOCKERFILE="$(cd "$(dirname "$DOCKERFILE")" && pwd)/$(basename "$DOCKERFILE")"
BUILD_CONTEXT="$(cd "$BUILD_CONTEXT" && pwd)"
ACR_LOGIN_SERVER="${ACR_NAME}.azurecr.io"
IMAGE_REF="${ACR_LOGIN_SERVER}/${SERVER_NAME}-server:${IMAGE_TAG}"

# Resolve the environment resource ID
ENV_ID=$(az containerapp env show \
    --resource-group "$RESOURCE_GROUP" \
    --name "$ACA_ENV_NAME" \
    --query id --output tsv)

echo "=== MCP Server ACA Deployment ==="
echo "  Server:         $SERVER_NAME"
echo "  Dockerfile:     $DOCKERFILE"
echo "  Build context:  $BUILD_CONTEXT"
echo "  Resource Group: $RESOURCE_GROUP"
echo "  ACR:            $ACR_LOGIN_SERVER"
echo "  ACA Env:        $ACA_ENV_NAME"
echo "  Image:          $IMAGE_REF"
echo "  Param file:     $PARAM_FILE"
echo ""

# ── 1. Build Docker image ────────────────────────────────────────────────────

if [[ "$DRY_RUN" == true ]]; then
    echo ">> [DRY RUN] Skipping Docker build and push."
    IMAGE_REF="${IMAGE_REF:-${ACR_LOGIN_SERVER}/${SERVER_NAME}-server:dry-run}"
else
    # Build the base image first if the server Dockerfile uses it (ARG BASE_IMAGE)
    BASE_DOCKERFILE="${REPO_ROOT}/deployment/base.Dockerfile"
    if [[ "$DOCKERFILE" != "$BASE_DOCKERFILE" && -f "$BASE_DOCKERFILE" ]]; then
        echo ">> Building base image (mcp-server-base:local)..."
        docker build \
            --file "$BASE_DOCKERFILE" \
            --tag "mcp-server-base:local" \
            "$BUILD_CONTEXT"
        echo ""
    fi

    echo ">> Building server image..."
    docker build \
        --file "$DOCKERFILE" \
        --tag "$IMAGE_REF" \
        "$BUILD_CONTEXT"

# ── 2. Push to ACR ───────────────────────────────────────────────────────────

    echo ">> Logging in to ACR..."
    az acr login --name "$ACR_NAME"

    echo ">> Pushing image to ACR..."
    docker push "$IMAGE_REF"
fi

# ── 3. Build extraEnvVars from .env.server ───────────────────────────────────────────
# Forward all .env.server variables to the Container App, excluding:
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

# Warn about forwarded variables
FORWARDED_KEYS=$(echo "$EXTRA_ENV_JSON" | python3 -c "import sys,json; keys=list(json.loads(sys.stdin.read()).keys()); print(', '.join(keys)) if keys else None")
if [[ -n "$FORWARDED_KEYS" && "$FORWARDED_KEYS" != "None" ]]; then
    echo ""
    echo "  WARNING: The above variables from .env.server will be visible in ACA"
    echo "  container configuration. Do not include secrets here unless intended."
fi
echo ""

# ── 4. Deploy Bicep ──────────────────────────────────────────────────────────

# Build storage parameters (only passed when configured)
STORAGE_PARAMS=""
if [[ -n "$STORAGE_LINK" ]]; then
    STORAGE_PARAMS="storageLink=$STORAGE_LINK cacheMountPath=$CACHE_MOUNT_PATH"
    echo "  Storage link:    $STORAGE_LINK"
    echo "  Cache mount:     $CACHE_MOUNT_PATH"
fi

if [[ "$DRY_RUN" == true ]]; then
    echo ">> [DRY RUN] Showing deployment what-if..."
    az deployment group what-if \
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
            $STORAGE_PARAMS
    echo ""
    echo "=== Dry run complete — no resources were modified ==="
else
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
            $STORAGE_PARAMS \
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
fi
