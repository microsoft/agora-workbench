# Chemistry MCP Server
#
# Prerequisites — build the base image first:
#   # From the repository root:
#   docker build -f deployment/base.Dockerfile -t mcp-server-base:local .
#
# Then build the chemistry server:
#   docker build -f examples/servers/chemistry/Dockerfile -t chemistry-server:local .

ARG BASE_IMAGE=mcp-server-base:local
FROM ${BASE_IMAGE}

# Copy chemistry package tree
COPY --chown=appuser:appuser examples/servers/chemistry /app/servers/chemistry
# Shared peer topology imported by the server config (servers.peers).
COPY --chown=appuser:appuser examples/servers/peers.py /app/servers/peers.py

# Pre-build conda environment (warm start) — avoids runtime install that can
# exceed ephemeral storage on container platforms like Azure Container Apps.
RUN python -m servers.chemistry.server.chemistry_server --warm

CMD ["python", "-m", "servers.chemistry.server.chemistry_server"]
