#!/bin/bash
set -euo pipefail
# Start the openLCA IPC server.
# Additional server options (e.g. -db <name> --readonly) can be appended by
# the Docker caller, e.g.:
#   docker run ... olca-ipc -db mydb --readonly
exec java \
  -XX:MaxRAMPercentage="${JAVA_MAX_RAM_PERCENTAGE:-80}" \
  -cp "/app/lib/*" \
  org.openlca.ipc.Server \
  -timeout 30 \
  -data /app/data \
  "$@"
