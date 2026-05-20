# Energy Systems MCP Server Dockerfile.
#
# Prerequisites — build the base image first:
#   cd src
#   docker build -f deployment/mcp_server/base.Dockerfile -t mcp-server-base:local .
#
# Then build this server image (from src/):
#   docker build -f domain_examples/energysystems/Dockerfile -t energysystems-server:local .

ARG BASE_IMAGE=mcp-server-base:local
FROM ${BASE_IMAGE}

# Copy the full domain tree (server, tools, skills, energysystems_tools package)
COPY --chown=appuser:appuser domain_examples/energysystems /app/domain_examples/energysystems

CMD ["python", "-m", "domain_examples.energysystems.server.energysystems_server"]
