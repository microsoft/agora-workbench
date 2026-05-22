# Chemistry MCP Server
#
# Prerequisites — build the base image first:
#   # From the repository root:
#   docker build -f deployment/mcp_server/base.Dockerfile -t mcp-server-base:local .
#
# Then build the chemistry server:
#   docker build -f examples/domain_examples/chemistry/Dockerfile -t chemistry-server:local .

ARG BASE_IMAGE=mcp-server-base:local
FROM ${BASE_IMAGE}

# Copy chemistry package tree so Python module execution can resolve the
# full package path consistently at runtime.
COPY --chown=appuser:appuser examples/domain_examples/chemistry /app/domain_examples/chemistry

CMD ["python", "-m", "domain_examples.chemistry.server.chemistry_server"]
