#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Deploy a single MCP server (or Activity UI) to Azure Container Apps.
#
# This script:
#   1. Loads infrastructure config from .env.server
#   2. Builds the Docker image for the requested server
#   3. Pushes the image to the existing Azure Container Registry
#   4. Deploys (or updates) the Container App via Bicep
#
# Prerequisites:
#   - az cli authenticated (`az login`)
#   - Docker daemon running
#   - .env.server configured with ACA_* variables (see .env.server.example)
#
# Usage:
#   ./deploy-server.sh --server chemistry
#   ./deploy-server.sh --server activity-ui --template activity-ui.bicep --skip-base-build
# ---------------------------------------------------------------------------

# shellcheck source=_deploy-common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_deploy-common.sh"

SERVER_NAME=""
PARAM_FILE=""
DOCKERFILE=""
BUILD_CONTEXT=""
TEMPLATE_FILE=""
SKIP_BASE_BUILD=false

usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Required:
  --server, -s          NAME    Server name (used for image naming and
                                parameters/<name>.bicepparam lookup)

Optional:
  --dockerfile, -f      PATH    Path to the Dockerfile to build
                                (default: auto-detected or deployment/base.Dockerfile)
  --context, -c         PATH    Docker build context directory (default: repo root)
  --template            PATH    Bicep template file (default: main.bicep)
  --skip-base-build             Skip building the mcp-server-base image
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
        --template)           TEMPLATE_FILE="$2";   shift 2 ;;
        --skip-base-build)    SKIP_BASE_BUILD=true; shift ;;
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

# ── Resolve defaults ─────────────────────────────────────────────────────────

if [[ -z "$DOCKERFILE" ]]; then
    SERVER_DOCKERFILE="${REPO_ROOT}/examples/servers/${SERVER_NAME}/Dockerfile"
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

validate_infra_vars
resolve_image_tag
resolve_acr

if [[ -z "$PARAM_FILE" ]]; then
    PARAM_FILE="$SCRIPT_DIR/parameters/${SERVER_NAME}.bicepparam"
fi

if [[ ! -f "$PARAM_FILE" ]]; then
    echo "ERROR: Parameter file not found: $PARAM_FILE" >&2
    echo "Create it in parameters/${SERVER_NAME}.bicepparam" >&2
    exit 1
fi

DOCKERFILE="$(cd "$(dirname "$DOCKERFILE")" && pwd)/$(basename "$DOCKERFILE")"
BUILD_CONTEXT="$(cd "$BUILD_CONTEXT" && pwd)"
IMAGE_REF="${ACR_LOGIN_SERVER}/${SERVER_NAME}:${IMAGE_TAG}"

if [[ -z "$TEMPLATE_FILE" ]]; then
    TEMPLATE_FILE="$SCRIPT_DIR/main.bicep"
fi

resolve_env_id

echo "=== ACA Deployment ==="
echo "  Server:         $SERVER_NAME"
echo "  Template:       $TEMPLATE_FILE"
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
    BASE_DOCKERFILE="${REPO_ROOT}/deployment/base.Dockerfile"
    if [[ "$SKIP_BASE_BUILD" == false && "$DOCKERFILE" != "$BASE_DOCKERFILE" && -f "$BASE_DOCKERFILE" ]]; then
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

# ── 3. Build extraEnvVars ────────────────────────────────────────────────────

EXTRA_ENV_JSON="$(build_extra_env_json)"

echo "  Extra env vars:  $(echo "$EXTRA_ENV_JSON" | python3 -c "import sys,json; print(', '.join(json.loads(sys.stdin.read()).keys()))")"
echo ""

# ── 4. Deploy Bicep ──────────────────────────────────────────────────────────

OPTIONAL_PARAMS=""
if [[ "$TEMPLATE_FILE" == *"main.bicep" ]]; then
    OPTIONAL_PARAMS="passthroughEnvVars=$EXTRA_ENV_JSON"
    if [[ -n "$STORAGE_LINK" ]]; then
        OPTIONAL_PARAMS+=" storageLink=$STORAGE_LINK cacheMountPath=$CACHE_MOUNT_PATH"
        echo "  Storage link:    $STORAGE_LINK"
        echo "  Cache mount:     $CACHE_MOUNT_PATH"
    fi
fi

# Activity UI has its own app registration
BICEP_ENTRA_CLIENT_ID="$ENTRA_CLIENT_ID_VAL"
if [[ "$TEMPLATE_FILE" == *"activity-ui"* && -n "$ACTIVITY_UI_CLIENT_ID_VAL" ]]; then
    BICEP_ENTRA_CLIENT_ID="$ACTIVITY_UI_CLIENT_ID_VAL"
    ACTIVITY_UI_AUDIENCE_VAL="${ACTIVITY_UI_AUDIENCE:-}"
    if [[ -n "$ACTIVITY_UI_AUDIENCE_VAL" ]]; then
        OPTIONAL_PARAMS+=" entraAudience=$ACTIVITY_UI_AUDIENCE_VAL"
    fi
fi

if [[ "$DRY_RUN" == true ]]; then
    echo ">> [DRY RUN] Showing deployment what-if..."
    az deployment group what-if \
        --resource-group "$RESOURCE_GROUP" \
        --template-file "$TEMPLATE_FILE" \
        --parameters "$PARAM_FILE" \
        --parameters \
            containerImage="$IMAGE_REF" \
            environmentId="$ENV_ID" \
            identityId="$IDENTITY_ID" \
            identityClientId="$IDENTITY_CLIENT_ID" \
            registryServer="$ACR_LOGIN_SERVER" \
            entraClientId="$BICEP_ENTRA_CLIENT_ID" \
            entraTenantId="$ENTRA_TENANT_ID_VAL" \
            $OPTIONAL_PARAMS
    echo ""
    echo "=== Dry run complete — no resources were modified ==="
else
    echo ">> Deploying Container App via Bicep..."
    DEPLOY_OUTPUT=$(az deployment group create \
        --resource-group "$RESOURCE_GROUP" \
        --template-file "$TEMPLATE_FILE" \
        --parameters "$PARAM_FILE" \
        --parameters \
            containerImage="$IMAGE_REF" \
            environmentId="$ENV_ID" \
            identityId="$IDENTITY_ID" \
            identityClientId="$IDENTITY_CLIENT_ID" \
            registryServer="$ACR_LOGIN_SERVER" \
            entraClientId="$BICEP_ENTRA_CLIENT_ID" \
            entraTenantId="$ENTRA_TENANT_ID_VAL" \
            $OPTIONAL_PARAMS \
        --query 'properties.outputs' \
        --output json)

# ── 5. Report ────────────────────────────────────────────────────────────────

    FQDN=$(echo "$DEPLOY_OUTPUT" | python3 -c "import sys,json; o=json.loads(sys.stdin.read()); print(o.get('fqdn',{}).get('value',''))" 2>/dev/null)

    if [[ -z "$FQDN" ]]; then
        FQDN=$(az containerapp show \
            --name "${SERVER_NAME}" \
            --resource-group "$RESOURCE_GROUP" \
            --query 'properties.configuration.ingress.fqdn' \
            --output tsv 2>/dev/null || true)
    fi
    if [[ -z "$FQDN" ]]; then
        FQDN=$(az containerapp show \
            --name "${SERVER_NAME}-server" \
            --resource-group "$RESOURCE_GROUP" \
            --query 'properties.configuration.ingress.fqdn' \
            --output tsv 2>/dev/null || true)
    fi

    # Update Activity UI app registration redirect URI
    if [[ "$TEMPLATE_FILE" == *"activity-ui"* && -n "$FQDN" && -n "$BICEP_ENTRA_CLIENT_ID" ]]; then
        REDIRECT_URI="https://${FQDN}/.auth/login/aad/callback"
        echo ""
        echo ">> Updating app registration redirect URI..."
        az ad app update --id "$BICEP_ENTRA_CLIENT_ID" \
            --web-redirect-uris "$REDIRECT_URI" \
            --output none
        echo "   Set to: $REDIRECT_URI"
    fi

    echo ""
    echo "=== Deployment complete ==="
    echo "  App URL:  https://${FQDN}"
    echo "  Health:   https://${FQDN}/health"
fi
