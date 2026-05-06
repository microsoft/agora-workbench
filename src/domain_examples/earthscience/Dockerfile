# Earth Science MCP Server
#
# Prerequisites — build the base image first:
#   cd src
#   docker build -f deployment/mcp_server/base.Dockerfile -t mcp-server-base:local .
#
# Then build the earth science server:
#   docker build -t earthscience-server:local .

FROM mcp-server-base:local

# Copy earth science server code
COPY --chown=appuser:appuser domain_examples/earthscience/server /app/domain_examples/earthscience/server
COPY --chown=appuser:appuser domain_examples/earthscience/__init__.py /app/domain_examples/earthscience/__init__.py

CMD ["python", "-m", "domain_examples.earthscience.server.earthscience_server"]
