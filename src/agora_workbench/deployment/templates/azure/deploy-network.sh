#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Deploy a connector network to Azure Container Apps.
#
# Reads a YAML manifest defining upstream servers and connectors, then deploys
# them in dependency order with health-gating between each step.
#
# Deploy order:
#   1. All upstream servers (health-checked after each)
#   2. Connectors in topological order based on depends_on (health-checked)
#
# Prerequisites:
#   - az cli authenticated (`az login`)
#   - Docker daemon running
#   - python3 with PyYAML installed
#   - .env.server configured with ACA_* variables
#
# Usage:
#   ./deploy-network.sh networks/science-hub.yaml
#   ./deploy-network.sh networks/science-hub-gateway.yaml --dry-run
# ---------------------------------------------------------------------------

# shellcheck source=_deploy-common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_deploy-common.sh"

usage() {
    cat <<EOF
Usage: $(basename "$0") MANIFEST [OPTIONS]

Arguments:
  MANIFEST              Path to network manifest YAML file

Optional:
  --resource-group, -g  NAME    Override ACA_RESOURCE_GROUP from .env.server
  --tag, -t             TAG     Docker image tag (default: git short SHA)
  --acr-name            NAME    Override ACA_ACR_NAME from .env.server
  --storage-link        NAME    ACA environment storage link name for Azure Files cache
  --cache-mount-path    PATH    Container mount path for cache (default: /home/appuser/.cache/mcp-envs)
  --dry-run                     Show what would be deployed without building or deploying
  -h, --help                    Show this help message
EOF
    exit 0
}

# ── Parse arguments ──────────────────────────────────────────────────────────

NETWORK_FILE=""

# First positional argument is the manifest file
if [[ $# -gt 0 && ! "$1" =~ ^-- ]]; then
    NETWORK_FILE="$1"
    shift
fi

while [[ $# -gt 0 ]]; do
    case $1 in
        --resource-group|-g)  RESOURCE_GROUP="$2";  shift 2 ;;
        --tag|-t)             IMAGE_TAG="$2";       shift 2 ;;
        --acr-name)           ACR_NAME="$2";        shift 2 ;;
        --storage-link)       STORAGE_LINK="$2";    shift 2 ;;
        --cache-mount-path)   CACHE_MOUNT_PATH="$2"; shift 2 ;;
        --dry-run)            DRY_RUN=true;         shift ;;
        -h|--help)            usage ;;
        *)
            if [[ -z "$NETWORK_FILE" ]]; then
                NETWORK_FILE="$1"; shift
            else
                echo "Unknown option: $1"; usage
            fi
            ;;
    esac
done

if [[ -z "$NETWORK_FILE" ]]; then
    echo "ERROR: Network manifest file is required." >&2
    usage
fi

# ── Resolve manifest path ────────────────────────────────────────────────────

if [[ "$NETWORK_FILE" = /* ]]; then
    MANIFEST_PATH="$NETWORK_FILE"
else
    MANIFEST_PATH="$SCRIPT_DIR/$NETWORK_FILE"
fi

if [[ ! -f "$MANIFEST_PATH" ]]; then
    echo "ERROR: Network manifest not found: $MANIFEST_PATH" >&2
    exit 1
fi

MANIFEST_DIR="$(cd "$(dirname "$MANIFEST_PATH")" && pwd)"

# ── Validate prerequisites ───────────────────────────────────────────────────

validate_infra_vars
resolve_image_tag
resolve_acr

PYTHON_CMD="$(command -v python3 || command -v python)"
if [[ -z "$PYTHON_CMD" ]]; then
    echo "ERROR: python3 is required for network deployments." >&2
    exit 1
fi

if ! "$PYTHON_CMD" -c "import yaml" 2>/dev/null; then
    echo "ERROR: PyYAML is required. Install it: pip install pyyaml" >&2
    exit 1
fi

# ── Helper functions ─────────────────────────────────────────────────────────

wait_for_health() {
    local server_name="$1"
    local internal_only="$2"
    local port="$3"
    local app_name="${server_name}-server"

    echo ">> Waiting for health: $app_name"
    local attempts=30
    local sleep_seconds=10

    for ((i=1; i<=attempts; i++)); do
        local fqdn
        fqdn="$(az containerapp show --name "$app_name" --resource-group "$RESOURCE_GROUP" \
            --query properties.configuration.ingress.fqdn -o tsv 2>/dev/null || true)"

        if [[ -n "$fqdn" ]]; then
            if curl -fsS --max-time 5 "https://${fqdn}/health" >/dev/null 2>&1 || \
               curl -fsS --max-time 5 "http://${fqdn}/health" >/dev/null 2>&1; then
                echo "   ✓ Healthy via ingress /health"
                return 0
            fi
        fi

        if [[ "$internal_only" == "true" ]]; then
            local exec_health_cmd
            exec_health_cmd="sh -lc 'curl -fsS http://localhost:${port}/health >/dev/null || wget -q -O- http://localhost:${port}/health >/dev/null'"
            if az containerapp exec \
                --name "$app_name" \
                --resource-group "$RESOURCE_GROUP" \
                --command "$exec_health_cmd" >/dev/null 2>&1; then
                echo "   ✓ Healthy via in-container /health"
                return 0
            fi
        fi

        echo "   ...attempt $i/$attempts"
        sleep "$sleep_seconds"
    done

    echo "ERROR: $app_name did not become healthy in time." >&2
    return 1
}

deploy_node() {
    local server_name="$1"
    local dockerfile_path="$2"
    local context_path="$3"
    local param_file="$4"
    local external_ingress="$5"
    local is_connector="$6"

    local image_ref="${ACR_LOGIN_SERVER}/${server_name}:${IMAGE_TAG}"

    echo "=== Deploy: $server_name ==="
    echo "  Dockerfile:     $dockerfile_path"
    echo "  Context:        $context_path"
    echo "  Image:          $image_ref"
    echo "  Params:         $param_file"
    echo "  External:       $external_ingress"
    echo "  Connector:      $is_connector"

    if [[ "$DRY_RUN" != true ]]; then
        # Build base image if this is a different Dockerfile that may depend on it
        BASE_DOCKERFILE="${REPO_ROOT}/deployment/base.Dockerfile"
        if [[ "$dockerfile_path" != "$BASE_DOCKERFILE" && -f "$BASE_DOCKERFILE" ]]; then
            docker build --file "$BASE_DOCKERFILE" --tag "mcp-server-base:local" "$context_path" --quiet
        fi

        docker build --file "$dockerfile_path" --tag "$image_ref" "$context_path"
        az acr login --name "$ACR_NAME" --output none
        docker push "$image_ref"
    else
        echo "   [DRY RUN] Skipping build/push"
    fi

    local extra_env_json
    extra_env_json="$(build_extra_env_json)"

    local env_id
    env_id=$(az containerapp env show --resource-group "$RESOURCE_GROUP" \
        --name "$ACA_ENV_NAME" --query id --output tsv)

    local -a storage_params=()
    if [[ "$is_connector" == "false" && -n "$STORAGE_LINK" ]]; then
        storage_params=("storageLink=$STORAGE_LINK" "cacheMountPath=$CACHE_MOUNT_PATH")
    fi

    if [[ "$DRY_RUN" == true ]]; then
        echo "   [DRY RUN] Would deploy via Bicep with externalIngress=$external_ingress"
    else
        az deployment group create \
            --resource-group "$RESOURCE_GROUP" \
            --template-file "$SCRIPT_DIR/main.bicep" \
            --parameters "$param_file" \
            --parameters \
                "containerImage=$image_ref" \
                "environmentId=$env_id" \
                "identityId=$IDENTITY_ID" \
                "identityClientId=$IDENTITY_CLIENT_ID" \
                "registryServer=$ACR_LOGIN_SERVER" \
                "entraClientId=$ENTRA_CLIENT_ID_VAL" \
                "entraTenantId=$ENTRA_TENANT_ID_VAL" \
                "externalIngress=$external_ingress" \
                "passthroughEnvVars=$extra_env_json" \
                "${storage_params[@]}" \
            --output none
    fi

    echo ""
}

# ── Parse manifest and deploy ────────────────────────────────────────────────

echo "=== Network Deployment ==="
echo "  Manifest:       $MANIFEST_PATH"
echo "  Resource Group: $RESOURCE_GROUP"
echo "  ACR:            $ACR_LOGIN_SERVER"
echo "  Image tag:      $IMAGE_TAG"
echo "  Dry run:        $DRY_RUN"
echo ""

mapfile -t nodes < <(
    "$PYTHON_CMD" - <<'PY' "$MANIFEST_PATH"
import sys
from pathlib import Path

import yaml

path = Path(sys.argv[1])
data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

# --- Parse connectors (support both singular and plural keys) ---
connector_single = data.get("connector")
connectors_list = data.get("connectors")

if connector_single and connectors_list:
    raise ValueError("network manifest cannot have both 'connector' and 'connectors' — use one or the other")

if connectors_list is not None:
    if not isinstance(connectors_list, list) or not connectors_list:
        raise ValueError("'connectors' must be a non-empty list")
    connectors = connectors_list
elif connector_single is not None:
    if not isinstance(connector_single, dict):
        raise ValueError("'connector' must be an object")
    connectors = [connector_single]
else:
    raise ValueError("network manifest requires a 'connector' or 'connectors' key")

# --- Parse upstreams ---
upstreams = data.get("upstreams", [])
if not isinstance(upstreams, list):
    raise ValueError("network manifest 'upstreams' must be a list")

# --- Validate connectors ---
connector_names = []
for i, c in enumerate(connectors):
    if not isinstance(c, dict):
        raise ValueError(f"connectors[{i}] must be an object")
    name = str(c.get("server", "")).strip()
    if not name:
        raise ValueError(f"connectors[{i}] requires a 'server' name")
    if name in connector_names:
        raise ValueError(f"duplicate connector server name: '{name}'")
    connector_names.append(name)

connector_name_set = set(connector_names)

for c in connectors:
    deps = c.get("depends_on", [])
    if not isinstance(deps, list):
        raise ValueError(f"connector '{c['server']}': depends_on must be a list")
    name = c["server"]
    for dep in deps:
        if dep == name:
            raise ValueError(f"connector '{name}' cannot depend on itself")
        if dep not in connector_name_set:
            raise ValueError(
                f"connector '{name}' depends on '{dep}' which is not a known connector. "
                f"Available: {connector_names}. Note: depends_on is for connector-to-connector "
                f"ordering only (upstreams are always deployed first)."
            )

# --- Topological sort (preserves manifest order as tie-breaker) ---
in_degree = {name: 0 for name in connector_names}
dependents = {name: [] for name in connector_names}

for c in connectors:
    name = c["server"]
    for dep in c.get("depends_on", []):
        in_degree[name] += 1
        dependents[dep].append(name)

queue = [name for name in connector_names if in_degree[name] == 0]
sorted_connectors = []

while queue:
    current = queue.pop(0)
    sorted_connectors.append(current)
    for dependent in dependents[current]:
        in_degree[dependent] -= 1
        if in_degree[dependent] == 0:
            queue.append(dependent)
    queue.sort(key=lambda n: connector_names.index(n))

if len(sorted_connectors) != len(connector_names):
    remaining = [n for n in connector_names if n not in sorted_connectors]
    raise ValueError(f"circular dependency detected among connectors: {remaining}")

connector_by_name = {c["server"]: c for c in connectors}

# --- Emit nodes ---
def emit(role: str, node: dict, internal_default: bool) -> None:
    server = str(node.get("server", "")).strip()
    params = str(node.get("params", "")).strip()
    if not server or not params:
        raise ValueError(f"{role} entries require 'server' and 'params'")

    internal = bool(node.get("internal", internal_default))
    dockerfile = str(node.get("dockerfile", "")).strip() or "_"
    context = str(node.get("context", "")).strip() or "_"
    port = int(node.get("port", 8000))

    print(f"{role}\t{server}\t{params}\t{str(internal).lower()}\t{dockerfile}\t{context}\t{port}")

for upstream in upstreams:
    if not isinstance(upstream, dict):
        raise ValueError("all upstream entries must be objects")
    emit("upstream", upstream, True)

for name in sorted_connectors:
    emit("connector", connector_by_name[name], False)
PY
)

for node in "${nodes[@]}"; do
    IFS=$'\t' read -r role server params internal dockerfile context port <<< "$node"

    # Resolve param file path
    if [[ "$params" = /* ]]; then
        resolved_param_file="$params"
    else
        resolved_param_file="$MANIFEST_DIR/$params"
    fi

    # Resolve Dockerfile ("_" is sentinel for unset)
    if [[ "$dockerfile" == "_" || -z "$dockerfile" ]]; then
        server_df="${REPO_ROOT}/examples/servers/${server}/Dockerfile"
        if [[ -f "$server_df" ]]; then
            dockerfile="$server_df"
        else
            dockerfile="${REPO_ROOT}/deployment/base.Dockerfile"
        fi
    elif [[ "$dockerfile" != /* ]]; then
        dockerfile="$MANIFEST_DIR/$dockerfile"
    fi

    # Resolve build context (default: repo root; "_" is sentinel for unset)
    if [[ "$context" == "_" || -z "$context" ]]; then
        context="$REPO_ROOT"
    elif [[ "$context" != /* ]]; then
        context="$MANIFEST_DIR/$context"
    fi

    # Determine ingress visibility
    external_ingress="true"
    if [[ "$internal" == "true" ]]; then
        external_ingress="false"
    fi

    is_connector="false"
    if [[ "$role" == "connector" ]]; then
        is_connector="true"
    fi

    deploy_node "$server" "$dockerfile" "$context" "$resolved_param_file" "$external_ingress" "$is_connector"

    # Health-check before deploying dependents (skip in dry-run)
    if [[ "$DRY_RUN" != true ]]; then
        wait_for_health "$server" "$internal" "$port"
    fi
done

echo "=== Network deploy complete ==="
