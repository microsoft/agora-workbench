# Chemistry MCP Server
#
# Prerequisites — build the base image first:
#   cd src
#   docker build -f deployment/mcp_server/base.Dockerfile -t mcp-server-base:local .
#
# Then build the chemistry server:
#   docker build -t chemistry-server:local .

ARG BASE_IMAGE=mcp-server-base:local
FROM ${BASE_IMAGE}

# Copy chemistry server code
COPY --chown=appuser:appuser domain_examples/chemistry/server /app/domain_examples/chemistry/server
COPY --chown=appuser:appuser domain_examples/chemistry/__init__.py /app/domain_examples/chemistry/__init__.py

CMD ["python", "-m", "domain_examples.chemistry.server.chemistry_server"]
