# Chemistry MCP Server
#
# Prerequisites — build the base image first:
#   cd src
#   docker build -f deployment/mcp_server/base.Dockerfile -t mcp-server-base:local .
#
# Then build the chemistry server:
#   docker build -t chemistry-server:local .

FROM mcp-server-base:local

# Copy chemistry package tree so Python module execution can resolve the
# full package path consistently at runtime.
COPY --chown=appuser:appuser domain_examples/chemistry /app/domain_examples/chemistry

CMD ["python", "-m", "domain_examples.chemistry.server.chemistry_server"]
