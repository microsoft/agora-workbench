#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Shared deployment functions and configuration for ACA deploy scripts.
#
# Sourced by deploy-server.sh and deploy-network.sh — not run directly.
# ---------------------------------------------------------------------------

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[1]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
# Root of the scaffolded deployment tree (the parent of azure/). Derived from
# SCRIPT_DIR rather than hardcoded as "deployment", since `init --output-dir`
# lets the directory be named anything.
DEPLOYMENT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="${SCRIPT_DIR}/../.env.server"

# ── Load .env.server ──────────────────────────────────────────────────────────

if [[ -f "$ENV_FILE" ]]; then
    set -a
    while IFS='=' read -r key value; do
        [[ "$key" =~ ^[[:space:]]*# ]] && continue
        [[ -z "$key" ]] && continue
        value="${value#\"}"; value="${value%\"}"
        value="${value#\'}"; value="${value%\'}"
        export "$key=$value"
    done < <(grep -v '^[[:space:]]*$' "$ENV_FILE")
    set +a
else
    echo "WARNING: .env.server not found at $ENV_FILE — using environment variables only." >&2
fi

# ── Defaults from .env.server (ACA_* variables) ──────────────────────────────

RESOURCE_GROUP="${ACA_RESOURCE_GROUP:-}"
ACR_NAME="${ACA_ACR_NAME:-agoramcpcr}"
ACA_ENV_NAME="${ACA_ENVIRONMENT_NAME:-agora-mcp-envs}"
IDENTITY_ID="${ACA_IDENTITY_ID:-}"
IDENTITY_CLIENT_ID="${ACA_IDENTITY_CLIENT_ID:-}"
ENTRA_CLIENT_ID_VAL="${ENTRA_CLIENT_ID:-}"
ENTRA_TENANT_ID_VAL="${ENTRA_TENANT_ID:-}"
ACTIVITY_UI_CLIENT_ID_VAL="${ACTIVITY_UI_CLIENT_ID:-}"
STORAGE_LINK="${ACA_STORAGE_LINK:-}"
CACHE_MOUNT_PATH="${ACA_CACHE_MOUNT_PATH:-/home/appuser/.cache/mcp-envs}"

DRY_RUN=false
IMAGE_TAG=""

# ── Shared functions ─────────────────────────────────────────────────────────

validate_infra_vars() {
    for var in RESOURCE_GROUP ACR_NAME ACA_ENV_NAME IDENTITY_ID IDENTITY_CLIENT_ID ENTRA_CLIENT_ID_VAL ENTRA_TENANT_ID_VAL; do
        if [[ -z "${!var}" ]]; then
            echo "ERROR: $var is not set. Configure ACA_* variables in .env.server or pass as flags." >&2
            exit 1
        fi
    done
}

# Resolve the base image Dockerfile and store it in BASE_DOCKERFILE.
#
# `agora-workbench-deploy init` writes it to <deployment>/docker/base.Dockerfile;
# older scaffolds kept it at <deployment>/base.Dockerfile, so that path is still
# accepted as a fallback. Returns 0 when a file was found, or 1 with
# BASE_DOCKERFILE set to the preferred location when none exists.
resolve_base_dockerfile() {
    local candidate
    for candidate in "${DEPLOYMENT_DIR}/docker/base.Dockerfile" "${DEPLOYMENT_DIR}/base.Dockerfile"; do
        if [[ -f "$candidate" ]]; then
            BASE_DOCKERFILE="$candidate"
            return 0
        fi
    done

    BASE_DOCKERFILE="${DEPLOYMENT_DIR}/docker/base.Dockerfile"
    return 1
}

# Absolutize a path without requiring realpath (not present by default on macOS).
# Paths whose parent directory does not exist are returned unchanged.
canonical_path() {
    local path="$1" dir base

    if [[ -d "$path" ]]; then
        (cd "$path" && pwd)
        return
    fi

    dir="$(dirname "$path")"
    base="$(basename "$path")"

    if [[ -d "$dir" ]]; then
        printf '%s/%s\n' "$(cd "$dir" && pwd)" "$base"
    else
        printf '%s\n' "$path"
    fi
}

# Build args selecting where the base image gets agora-workbench from. Defaults
# to the Dockerfile's own default (the published package); export
# AGORA_WORKBENCH_SOURCE=local to build against a workbench checkout.
# Sets the WORKBENCH_SOURCE_ARGS array (a function can't return one).
WORKBENCH_SOURCE_ARGS=()
set_workbench_source_args() {
    WORKBENCH_SOURCE_ARGS=()
    if [[ -n "${AGORA_WORKBENCH_SOURCE:-}" ]]; then
        WORKBENCH_SOURCE_ARGS=(--build-arg "AGORA_WORKBENCH_SOURCE=${AGORA_WORKBENCH_SOURCE}")
    fi
}

resolve_image_tag() {
    if [[ -z "$IMAGE_TAG" ]]; then
        IMAGE_TAG="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo 'latest')"
    fi
}

resolve_acr() {
    ACR_LOGIN_SERVER="${ACR_NAME}.azurecr.io"
}

resolve_env_id() {
    ENV_ID=$(az containerapp env show \
        --resource-group "$RESOURCE_GROUP" \
        --name "$ACA_ENV_NAME" \
        --query id --output tsv)
}

# Build extraEnvVars JSON from .env.server for Bicep deployment.
# Forwards all runtime config vars, excluding infra-only and already-handled vars.
build_extra_env_json() {
    local extra_env_json="{}"
    if [[ -f "$ENV_FILE" ]]; then
        extra_env_json="$(
            python3 - <<'PY' "$ENV_FILE"
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
exclude = {'MCP_SERVER_ENTRA_CLIENT_ID','MCP_SERVER_ENTRA_TENANT_ID','MCP_SERVER_TRANSPORT','MCP_SERVER_PORT','OBO_SIMULATION_MODE'}
out = {k: v for k, v in sorted(env.items()) if k not in exclude and not k.startswith('ACA_') and not k.startswith('ENTRA_') and v}
print(json.dumps(out, separators=(',', ':')))
PY
        )"
    fi
    echo "$extra_env_json"
}
