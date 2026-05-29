# Earth Science MCP Server
#
# Prerequisites — build the base image first:
#   # From the repository root:
#   docker build -f deployment/base.Dockerfile -t mcp-server-base:local .
#
# Then build the earth science server:
#   docker build -t earthscience-server:local .

ARG BASE_IMAGE=mcp-server-base:local
FROM ${BASE_IMAGE}

# Copy the earthscience package tree so Python module execution can resolve
# the full package path consistently at runtime.
COPY --chown=appuser:appuser examples/domain_examples/earthscience /app/domain_examples/earthscience

# Pre-build environment (warm start)
RUN python -m domain_examples.earthscience.server.earthscience_server --warm

CMD ["python", "-m", "domain_examples.earthscience.server.earthscience_server"]
