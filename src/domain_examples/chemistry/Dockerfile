# Chemistry MCP Server
#
# Prerequisites — build the base image first:
#   cd src
#   docker build -f deployment/mcp_server/base.Dockerfile -t mcp-server-base:local .
#
# Then build the chemistry server:
#   docker build -f domain_examples/chemistry/Dockerfile -t chemistry-server:local .

ARG BASE_IMAGE=mcp-server-base:local
FROM ${BASE_IMAGE}

# Copy chemistry package tree so Python module execution can resolve the
# full package path consistently at runtime.
COPY --chown=appuser:appuser domain_examples/chemistry /app/domain_examples/chemistry

# Pre-build the chemistry conda environment in the image so container startup
# does not depend on network/DNS availability.
RUN mamba env create \
      --prefix /home/appuser/.cache/mcp-envs/chemistry/conda \
      --file /app/domain_examples/chemistry/environment.yml \
      --yes \
    && /home/appuser/.cache/mcp-envs/chemistry/conda/bin/python -m pip install --no-deps /app/domain_examples/chemistry/chemistry_tools

CMD ["python", "-m", "domain_examples.chemistry.server.chemistry_server"]
