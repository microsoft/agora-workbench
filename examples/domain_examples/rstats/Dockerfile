# R Statistics MCP Server
#
# Prerequisites — build the base image first:
#   # From the repository root:
#   docker build -f deployment/base.Dockerfile -t mcp-server-base:local .
#
# Then build the rstats server:
#   docker build -t rstats-server:local .

ARG BASE_IMAGE=mcp-server-base:local
FROM ${BASE_IMAGE}

# Copy the rstats package tree so Python module execution can resolve the full
# package path consistently at runtime.
COPY --chown=appuser:appuser examples/domain_examples/rstats /app/domain_examples/rstats

# Pre-build environment (warm start): builds the conda env (r-base + r-irkernel
# + R packages) and registers the IRkernel R kernel so the first request is fast.
RUN python -m domain_examples.rstats.server.rstats_server --warm

CMD ["python", "-m", "domain_examples.rstats.server.rstats_server"]
