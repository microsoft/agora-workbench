# Chemistry MCP Server
#
# Prerequisites — build the base image first:
#   # From the repository root:
#   docker build -f deployment/base.Dockerfile -t mcp-server-base:local .
#
# Then build the chemistry server:
#   docker build -f examples/domain_examples/chemistry/Dockerfile -t chemistry-server:local .

ARG BASE_IMAGE=mcp-server-base:local
FROM ${BASE_IMAGE}

# Copy warm config and chemistry_tools first (changes less often than server code)
# to preserve the expensive conda build layer across server code edits.
COPY --chown=appuser:appuser examples/domain_examples/chemistry/warm_config.yaml /app/domain_examples/chemistry/warm_config.yaml
COPY --chown=appuser:appuser examples/domain_examples/chemistry/chemistry_tools /app/domain_examples/chemistry/chemistry_tools

# Pre-build conda environment (warm start) — avoids runtime install that can
# exceed ephemeral storage on container platforms like Azure Container Apps.
RUN python -m code_execution.cli warm --config /app/domain_examples/chemistry/warm_config.yaml

# Copy the full chemistry package tree (server code, tools metadata, skills, etc.)
COPY --chown=appuser:appuser examples/domain_examples/chemistry /app/domain_examples/chemistry

CMD ["python", "-m", "domain_examples.chemistry.server.chemistry_server"]
