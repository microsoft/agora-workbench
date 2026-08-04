# Earth Science MCP Server
#
# Prerequisites — build the base image first:
#   # From the repository root:
#   docker build -f src/agora_workbench/deployment/templates/docker/base.Dockerfile --build-arg AGORA_WORKBENCH_SOURCE=local -t mcp-server-base:local .
#
# Then build the earth science server:
#   docker build -t earthscience-server:local .

ARG BASE_IMAGE=mcp-server-base:local
FROM ${BASE_IMAGE}

# Copy the earthscience package tree so Python module execution can resolve
# the full package path consistently at runtime.
COPY --chown=appuser:appuser examples/servers/earthscience /app/servers/earthscience
# Shared peer topology imported by the server config (servers.peers).
COPY --chown=appuser:appuser examples/servers/peers.py /app/servers/peers.py

# Pre-build environment (warm start)
RUN python -m servers.earthscience.server.earthscience_server --warm

CMD ["python", "-m", "servers.earthscience.server.earthscience_server"]
