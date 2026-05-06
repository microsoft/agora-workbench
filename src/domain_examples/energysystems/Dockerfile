# Energy Systems MCP Server Dockerfile.
#
# Prerequisites — build the base image first:
#   cd src
#   docker build -f deployment/mcp_server/base.Dockerfile -t mcp-server-base:local .
#
# Then build this server image:
#   docker build -t energysystems-server:local .

FROM mcp-server-base:local

# Copy server code into the image
COPY --chown=appuser:appuser server/ /app/domain_examples/energysystems/server/

CMD ["python", "-m", "domain_examples.energysystems.server.energysystems_server"]
