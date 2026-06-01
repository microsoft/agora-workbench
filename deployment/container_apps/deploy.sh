#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Deploy MCP code execution servers to Azure Container Apps.
#
# This script supports:
#   - Single-server deployment (existing behavior)
#   - Network deployment via manifest (upstreams first, connector last)
# ---------------------------------------------------------------------------

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Parse .env.server safely (do NOT source it as shell code).
# Extract ACA_*, MCP_SERVER_*, and ENTRA_* for use as shell variables.
if [[ -f "$SCRIPT_DIR/../.env.server" ]]; then
    while IFS='=' read -r key val; do
        [[ -z "$key" ]] && continue
        # Only import known prefixes needed by deploy.sh itself
        case "$key" in
            ACA_*|MCP_SERVER_ENTRA_*|ENTRA_CLIENT_ID|ENTRA_TENANT_ID)
                export "$key=$val"
                ;;
        esac
    done < <(
        python - <<'PY' "$SCRIPT_DIR/../.env.server"
import re, sys
from pathlib import Path

path = Path(sys.argv[1])
line_re = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$')

for raw in path.read_text(encoding='utf-8').splitlines():
    s = raw.strip()
    if not s or s.startswith('#'):
        continue
    m = line_re.match(raw)
    if not m:
        continue
    key, val = m.group(1), m.group(2).strip()
    if val and val[0] not in ('"', "'") and ' #' in val:
        val = val.split(' #', 1)[0].rstrip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
        val = val[1:-1]
    print(f"{key}={val}")
PY
    )
fi

usage() {
    cat <<EOF_USAGE
Usage: $(basename "$0") [options]

Single-server mode (required):
  --server NAME            Server name (e.g. chemistry)
  --dockerfile PATH        Path to Dockerfile (optional; defaults by server name)
  --context PATH           Build context directory (optional; defaults to repo root)

Network mode (required):
  --network PATH           Network manifest YAML (e.g. networks/science-hub.yaml)

Optional:
  --tag TAG                Image tag (default: latest)
  --resource-group, -g NAME   Override ACA_RESOURCE_GROUP from .env.server
  --acr-name NAME             Override ACA_ACR_NAME from .env.server
  --storage-link NAME         Azure Files storage link name (overrides ACA_STORAGE_LINK)
  --cache-mount-path PATH     Cache mount path in container (overrides ACA_CACHE_MOUNT_PATH)
  --dry-run                    Show what would run; do not build or deploy
  --help                       Show this help

Examples:
  $(basename "$0") --server chemistry
  $(basename "$0") --server chemistry --dockerfile ../../examples/domain_examples/chemistry/Dockerfile --context ../..
  $(basename "$0") --network networks/science-hub.yaml
EOF_USAGE
}

SERVER_NAME=""
DOCKERFILE_PATH=""
CONTEXT_PATH=""
NETWORK_FILE=""
TAG="latest"
DRY_RUN=false

RESOURCE_GROUP="${ACA_RESOURCE_GROUP:-}"
ACR_NAME="${ACA_ACR_NAME:-agoramcpcr}"
ACA_ENV_NAME="${ACA_ENVIRONMENT_NAME:-agora-mcp-envs}"
IDENTITY_ID="${ACA_IDENTITY_ID:-}"
IDENTITY_CLIENT_ID="${ACA_IDENTITY_CLIENT_ID:-}"

# Optional Azure Files env-cache wiring
STORAGE_LINK="${ACA_STORAGE_LINK:-}"
CACHE_MOUNT_PATH="${ACA_CACHE_MOUNT_PATH:-/home/appuser/.cache/mcp-envs}"

# Entra auth settings from .env.server
ENTRA_CLIENT_ID_VAL="${MCP_SERVER_ENTRA_CLIENT_ID:-${ENTRA_CLIENT_ID:-}}"
ENTRA_TENANT_ID_VAL="${MCP_SERVER_ENTRA_TENANT_ID:-${ENTRA_TENANT_ID:-}}"

require_python() {
    if ! command -v python >/dev/null 2>&1; then
        echo "ERROR: python is required by deploy.sh for .env/network parsing." >&2
        exit 1
    fi
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --server)
            SERVER_NAME="$2"
            shift 2
            ;;
        --dockerfile)
            DOCKERFILE_PATH="$2"
            shift 2
            ;;
        --context)
            CONTEXT_PATH="$2"
            shift 2
            ;;
        --network)
            NETWORK_FILE="$2"
            shift 2
            ;;
        --tag)
            TAG="$2"
            shift 2
            ;;
        --resource-group|-g)
            RESOURCE_GROUP="$2"
            shift 2
            ;;
        --acr-name)
            ACR_NAME="$2"
            shift 2
            ;;
        --storage-link)
            STORAGE_LINK="$2"
            shift 2
            ;;
        --cache-mount-path)
            CACHE_MOUNT_PATH="$2"
            shift 2
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            echo "Unknown arg: $1" >&2
            usage
            exit 1
            ;;
    esac
done

if [[ -n "$NETWORK_FILE" && -n "$SERVER_NAME" ]]; then
    echo "ERROR: --network cannot be used with --server/--dockerfile/--context" >&2
    exit 1
fi

if [[ -z "$NETWORK_FILE" && -z "$SERVER_NAME" ]]; then
    echo "ERROR: --server is required unless --network is used." >&2
    usage
    exit 1
fi

# Require critical infra/auth values
for var in RESOURCE_GROUP ACR_NAME ACA_ENV_NAME IDENTITY_ID IDENTITY_CLIENT_ID ENTRA_CLIENT_ID_VAL ENTRA_TENANT_ID_VAL; do
    if [[ -z "${!var}" ]]; then
        echo "ERROR: $var is not set. Configure ACA_* variables in .env.server or pass as flags." >&2
        exit 1
    fi
done

# Resolve dynamic values once
SUBSCRIPTION_ID="$(az account show --query id -o tsv)"
ACR_LOGIN_SERVER="$(az acr show --name "$ACR_NAME" --resource-group "$RESOURCE_GROUP" --query loginServer -o tsv)"
ENV_ID="$(az containerapp env show --name "$ACA_ENV_NAME" --resource-group "$RESOURCE_GROUP" --query id -o tsv)"

# Build passthroughEnvVars JSON object from deployment/.env.server
# Forward all runtime configuration variables to the container, excluding:
#   - ACA_*: infra-only variables consumed by deploy.sh itself
#   - MCP_SERVER_ENTRA_CLIENT_ID / MCP_SERVER_ENTRA_TENANT_ID (mapped to top-level Bicep params)
#   - MCP_SERVER_TRANSPORT (deployment enforces streamable HTTP)
#   - MCP_SERVER_PORT      (controlled by bicepparam/containerPort)
PASSTHROUGH_ENV_JSON='{}'
if [[ -f "$SCRIPT_DIR/../.env.server" ]]; then
    require_python
    PASSTHROUGH_ENV_JSON="$(
        python - <<'PY' "$SCRIPT_DIR/../.env.server"
import json, re, sys
from pathlib import Path

path = Path(sys.argv[1])
env = {}
line_re = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$')

for raw in path.read_text(encoding='utf-8').splitlines():
    s = raw.strip()
    if not s or s.startswith('#'):
        continue
    m = line_re.match(raw)
    if not m:
        continue
    key, val = m.group(1), m.group(2).strip()

    if val and val[0] not in ('"', "'") and ' #' in val:
        val = val.split(' #', 1)[0].rstrip()

    if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
        val = val[1:-1]

    env[key] = val

exclude_exact = {
    'MCP_SERVER_ENTRA_CLIENT_ID',
    'MCP_SERVER_ENTRA_TENANT_ID',
    'MCP_SERVER_TRANSPORT',
    'MCP_SERVER_PORT',
}

out = {}
for key in sorted(env):
    if key in exclude_exact:
        continue
    if key.startswith('ACA_'):
        continue
    out[key] = env[key]

print(json.dumps(out, separators=(',', ':')))
PY
    )"
fi

deploy_server() {
    local server_name="$1"
    local dockerfile_path="$2"
    local context_path="$3"
    local param_file="$4"
    local external_ingress="$5"
    local is_connector="$6"

    local resolved_dockerfile resolved_context
    resolved_dockerfile="$(realpath "$dockerfile_path")"
    resolved_context="$(realpath "$context_path")"

    if [[ ! -f "$param_file" ]]; then
        echo "ERROR: Missing parameter file: $param_file" >&2
        exit 1
    fi

    local image_ref
    image_ref="${ACR_LOGIN_SERVER}/${server_name}:${TAG}"

    echo "=== Deploy MCP Server: $server_name ==="
    echo "  Subscription:   $SUBSCRIPTION_ID"
    echo "  Resource Group: $RESOURCE_GROUP"
    echo "  ACR:            $ACR_NAME"
    echo "  ACA Env:        $ACA_ENV_NAME"
    echo "  Dockerfile:     $resolved_dockerfile"
    echo "  Context:        $resolved_context"
    echo "  Image:          $image_ref"
    echo "  Params:         $param_file"
    echo "  External:       $external_ingress"
    echo "  Connector:      $is_connector"
    echo "  Dry run:        $DRY_RUN"

    if [[ "$DRY_RUN" != true ]]; then
        echo ">> Building image..."
        docker build -f "$resolved_dockerfile" -t "$image_ref" "$resolved_context"

        echo ">> Logging into ACR..."
        az acr login --name "$ACR_NAME"

        echo ">> Pushing image..."
        docker push "$image_ref"
    else
        echo ">> Skipping build/push (dry-run)"
    fi

    local -a storage_params
    storage_params=()
    if [[ "$is_connector" == "false" && -n "$STORAGE_LINK" ]]; then
        storage_params+=("storageLink=$STORAGE_LINK" "cacheMountPath=$CACHE_MOUNT_PATH")
        echo ">> Including Azure Files cache mount: name=$STORAGE_LINK path=$CACHE_MOUNT_PATH"
    else
        echo ">> Skipping Azure Files cache mount"
    fi

    local -a deploy_params
    deploy_params=(
        "containerImage=$image_ref"
        "environmentId=$ENV_ID"
        "identityId=$IDENTITY_ID"
        "identityClientId=$IDENTITY_CLIENT_ID"
        "registryServer=$ACR_LOGIN_SERVER"
        "entraClientId=$ENTRA_CLIENT_ID_VAL"
        "entraTenantId=$ENTRA_TENANT_ID_VAL"
        "passthroughEnvVars=$PASSTHROUGH_ENV_JSON"
        "externalIngress=$external_ingress"
    )

    if [[ "$DRY_RUN" == true ]]; then
        echo ">> Running Bicep what-if..."
        az deployment group what-if \
            --resource-group "$RESOURCE_GROUP" \
            --template-file "$SCRIPT_DIR/main.bicep" \
            --parameters "$param_file" \
            --parameters \
            "${deploy_params[@]}" \
            "${storage_params[@]}"
    else
        echo ">> Deploying Container App via Bicep..."
        az deployment group create \
            --resource-group "$RESOURCE_GROUP" \
            --template-file "$SCRIPT_DIR/main.bicep" \
            --parameters "$param_file" \
            --parameters \
            "${deploy_params[@]}" \
            "${storage_params[@]}"
    fi

    echo ""
}

wait_for_upstream_health() {
    local server_name="$1"
    local internal_only="$2"
    local port="$3"
    local app_name="${server_name}-server"

    echo ">> Waiting for upstream health: $app_name"
    local attempts=30
    local sleep_seconds=10

    for ((i=1; i<=attempts; i++)); do
        local fqdn
        fqdn="$(az containerapp show --name "$app_name" --resource-group "$RESOURCE_GROUP" --query properties.configuration.ingress.fqdn -o tsv 2>/dev/null || true)"

        if [[ -n "$fqdn" ]]; then
            if curl -fsS --max-time 5 "https://${fqdn}/health" >/dev/null 2>&1 || curl -fsS --max-time 5 "http://${fqdn}/health" >/dev/null 2>&1; then
                echo "   ✓ Upstream healthy via ingress /health"
                return 0
            fi
        fi

        if [[ "$internal_only" == "true" ]]; then
            # For internal-only ingress, probe from inside the app container.
            # Some images include curl; others only ship wget.
            local exec_health_cmd
            exec_health_cmd="sh -lc 'curl -fsS http://localhost:${port}/health >/dev/null || wget -q -O- http://localhost:${port}/health >/dev/null'"
            if az containerapp exec \
                --name "$app_name" \
                --resource-group "$RESOURCE_GROUP" \
                --command "$exec_health_cmd" >/dev/null 2>&1; then
                echo "   ✓ Upstream healthy via in-container /health"
                return 0
            fi
        fi

        echo "   ...attempt $i/$attempts"
        sleep "$sleep_seconds"
    done

    echo "ERROR: Upstream $app_name did not become healthy in time." >&2
    return 1
}

default_dockerfile_for_server() {
    local server_name="$1"
    local candidate="$REPO_ROOT/examples/domain_examples/${server_name}/Dockerfile"
    if [[ ! -f "$candidate" ]]; then
        echo "ERROR: No default Dockerfile found for server '$server_name' at $candidate" >&2
        echo "Provide dockerfile/context in the network manifest for this server." >&2
        exit 1
    fi
    echo "$candidate"
}

run_network_manifest() {
    local network_file="$1"
    local manifest_path

    if [[ "$network_file" = /* ]]; then
        manifest_path="$network_file"
    else
        manifest_path="$SCRIPT_DIR/$network_file"
    fi

    if [[ ! -f "$manifest_path" ]]; then
        echo "ERROR: Network manifest not found: $manifest_path" >&2
        exit 1
    fi

    local manifest_dir
    manifest_dir="$(cd "$(dirname "$manifest_path")" && pwd)"

    require_python
    if ! python - <<'PY' >/dev/null 2>&1
import yaml
PY
    then
        echo "ERROR: PyYAML is required for --network manifests. Install it (e.g. 'python -m pip install pyyaml')." >&2
        exit 1
    fi

    mapfile -t nodes < <(
        python - <<'PY' "$manifest_path"
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

# Validate depends_on references
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
# Kahn's algorithm with stable ordering
in_degree = {name: 0 for name in connector_names}
dependents = {name: [] for name in connector_names}

for c in connectors:
    name = c["server"]
    for dep in c.get("depends_on", []):
        in_degree[name] += 1
        dependents[dep].append(name)

# Start with zero in-degree nodes, in manifest order
queue = [name for name in connector_names if in_degree[name] == 0]
sorted_connectors = []

while queue:
    # Pick the first node in manifest order among ready nodes
    current = queue.pop(0)
    sorted_connectors.append(current)
    for dependent in dependents[current]:
        in_degree[dependent] -= 1
        if in_degree[dependent] == 0:
            queue.append(dependent)
    # Re-sort queue by original manifest order for determinism
    queue.sort(key=lambda n: connector_names.index(n))

if len(sorted_connectors) != len(connector_names):
    remaining = [n for n in connector_names if n not in sorted_connectors]
    raise ValueError(f"circular dependency detected among connectors: {remaining}")

# Build lookup for connector dicts by server name
connector_by_name = {c["server"]: c for c in connectors}

# --- Emit nodes: upstreams first, then connectors in topo order ---
def emit(role: str, node: dict, internal_default: bool) -> None:
    server = str(node.get("server", "")).strip()
    params = str(node.get("params", "")).strip()
    if not server or not params:
        raise ValueError(f"{role} entries require 'server' and 'params'")

    internal = bool(node.get("internal", internal_default))
    dockerfile = str(node.get("dockerfile", "")).strip()
    context = str(node.get("context", "")).strip()
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

        local resolved_param_file
        if [[ "$params" = /* ]]; then
            resolved_param_file="$params"
        else
            resolved_param_file="$manifest_dir/$params"
        fi

        if [[ -z "$dockerfile" ]]; then
            dockerfile="$(default_dockerfile_for_server "$server")"
        elif [[ "$dockerfile" != /* ]]; then
            dockerfile="$manifest_dir/$dockerfile"
        fi

        if [[ -z "$context" ]]; then
            context="$REPO_ROOT"
        elif [[ "$context" != /* ]]; then
            context="$manifest_dir/$context"
        fi

        local external_ingress="true"
        if [[ "$internal" == "true" ]]; then
            external_ingress="false"
        fi

        local is_connector="false"
        if [[ "$role" == "connector" ]]; then
            is_connector="true"
        fi

        deploy_server "$server" "$dockerfile" "$context" "$resolved_param_file" "$external_ingress" "$is_connector"

        # Health-check all nodes before deploying their dependents
        wait_for_upstream_health "$server" "$internal" "$port"
    done
}

if [[ -n "$NETWORK_FILE" ]]; then
    run_network_manifest "$NETWORK_FILE"
    echo "=== Network deploy complete ==="
else
    PARAM_FILE="$SCRIPT_DIR/parameters/${SERVER_NAME}.bicepparam"
    if [[ -z "$DOCKERFILE_PATH" ]]; then
        DOCKERFILE_PATH="$(default_dockerfile_for_server "$SERVER_NAME")"
    fi
    if [[ -z "$CONTEXT_PATH" ]]; then
        CONTEXT_PATH="$REPO_ROOT"
    fi
    deploy_server "$SERVER_NAME" "$DOCKERFILE_PATH" "$CONTEXT_PATH" "$PARAM_FILE" "true" "false"
    echo "=== Deploy complete ==="
fi
