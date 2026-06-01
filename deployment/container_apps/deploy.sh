#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Deploy dispatcher — routes to deploy-server.sh or deploy-network.sh.
#
# This script exists for backward compatibility. Prefer calling the specific
# scripts directly:
#   ./deploy-server.sh --server chemistry
#   ./deploy-network.sh networks/science-hub.yaml
# ---------------------------------------------------------------------------

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Detect mode from arguments
for arg in "$@"; do
    if [[ "$arg" == "--network" ]]; then
        # Extract the manifest file (next arg after --network)
        ARGS=()
        MANIFEST=""
        while [[ $# -gt 0 ]]; do
            case $1 in
                --network)
                    if [[ $# -lt 2 || "$2" == --* ]]; then
                        echo "Error: --network requires a manifest file path" >&2
                        exit 1
                    fi
                    MANIFEST="$2"
                    shift 2
                    ;;
                *)
                    ARGS+=("$1")
                    shift
                    ;;
            esac
        done
        exec "$SCRIPT_DIR/deploy-network.sh" "$MANIFEST" "${ARGS[@]}"
    fi
done

# Default: single-server mode
exec "$SCRIPT_DIR/deploy-server.sh" "$@"
