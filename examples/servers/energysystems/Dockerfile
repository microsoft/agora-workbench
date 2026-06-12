# Energy Systems MCP Server Dockerfile.
#
# Prerequisites — build the base image first:
#   # From the repository root:
#   docker build -f deployment/base.Dockerfile -t mcp-server-base:local .
#
# Then build this server image (from src/):
#   docker build -f examples/servers/energysystems/Dockerfile -t energysystems-server:local .

ARG BASE_IMAGE=mcp-server-base:local
FROM ${BASE_IMAGE}

# Copy the full domain tree (server, tools, skills, energysystems_tools package)
COPY --chown=appuser:appuser examples/servers/energysystems /app/servers/energysystems

# Pre-build environment (warm start)
RUN python -m servers.energysystems.server.energysystems_server --warm

# linopy (a PyPSA dependency) probes for external solvers at import time by
# shelling out to `which glpsol` / `which cbc`. The minimal base image omits
# the `which` binary, so `import pypsa` raises FileNotFoundError and every
# kernel execution fails. Azure Linux has no standalone `which` package, so
# install a tiny POSIX shim backed by the shell builtin `command -v`: it prints
# the resolved path and exits 0 when found, exits 1 otherwise — enough for
# linopy's `returncode == 0` solver probe. Placed after --warm so the cached
# COPY/warm layers are preserved on rebuild.
USER root
RUN printf '#!/bin/sh\ncommand -v "$1"\n' > /usr/local/bin/which && chmod 0755 /usr/local/bin/which
USER appuser

CMD ["python", "-m", "servers.energysystems.server.energysystems_server"]
