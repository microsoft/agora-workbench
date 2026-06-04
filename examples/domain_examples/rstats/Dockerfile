# R Statistics MCP Server
#
# Prerequisites — build the Debian base image first. R domains require it:
# conda-forge R cannot spawn subprocesses (Sys.which / IRkernel startup) on the
# Azure Linux base, but works on Debian. See deployment/base.debian.Dockerfile.
#   # From the repository root:
#   docker build -f deployment/base.debian.Dockerfile -t mcp-server-base-debian:local .
#
# Then build the rstats server:
#   docker build -t rstats-server:local .

ARG BASE_IMAGE=mcp-server-base-debian:local
FROM ${BASE_IMAGE}

# Copy the rstats package tree so Python module execution can resolve the full
# package path consistently at runtime.
COPY --chown=appuser:appuser examples/domain_examples/rstats /app/domain_examples/rstats

# Pre-build environment (warm start): builds the conda env (r-base + r-irkernel
# + R packages) and registers the IRkernel R kernel so the first request is fast.
RUN python -m domain_examples.rstats.server.rstats_server --warm

CMD ["python", "-m", "domain_examples.rstats.server.rstats_server"]
