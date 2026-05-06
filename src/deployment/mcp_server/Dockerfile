# Multi-stage Dockerfile for code execution servers.
# Use --target to build specific server images.
#
# Domain stages are added by domain authors on top of the shared `base` image.
# To scaffold a new domain:
#   uv run python src/deployment/mcp_server/build.py new <name>
# To regenerate this file from domain configs:
#   uv run python src/deployment/mcp_server/build.py generate

# ============================================================================
# Stage: Base image with common dependencies
# ============================================================================
FROM mcr.microsoft.com/devcontainers/python:3.11 AS base

WORKDIR /app

# Remove problematic Yarn repository that has GPG key issues
RUN rm -f /etc/apt/sources.list.d/yarn.list

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    git \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh && \
    mv /root/.local/bin/uv /usr/local/bin/uv && \
    mv /root/.local/bin/uvx /usr/local/bin/uvx

# Install mamba with version pinning
ARG MINIFORGE_VERSION=25.11.0-1
RUN curl -L -O "https://github.com/conda-forge/miniforge/releases/download/${MINIFORGE_VERSION}/Miniforge3-Linux-x86_64.sh" && \
    bash Miniforge3-Linux-x86_64.sh -b -p /opt/miniforge3 && \
    rm Miniforge3-Linux-x86_64.sh
ENV PATH="/opt/miniforge3/bin:$PATH"

# Ensure pip is up to date
RUN python -m pip install --upgrade pip

# Pre-download wheels for Jupyter kernel stack to speed up runtime env builds.
# The CodeExecutionServer registers an ipykernel, and ipykernel pulls in IPython.
# Having these wheels locally avoids repeated downloads during environment creation.
RUN mkdir -p /opt/wheelhouse && \
    python -m pip download --dest /opt/wheelhouse \
    "ipykernel>=6.29.0" \
    && ls -1 /opt/wheelhouse | head -n 5

# Copy shared code (used by all servers)
COPY code_execution/code_execution /app/code_execution
COPY code_execution/requirements.txt /app/requirements.txt
COPY middleware/__init__.py /app/middleware/__init__.py
COPY middleware/tool_learning /app/middleware/tool_learning

# Install Python dependencies
RUN pip install --no-input -r /app/requirements.txt

# Create cache directory for MCP environments and add a non-root user for runtime
RUN useradd -m -d /home/appuser -s /bin/bash appuser && \
    mkdir -p /home/appuser/.cache/mcp-envs && \
    chown -R appuser:appuser /app /home/appuser /opt/wheelhouse

# Add /app to PYTHONPATH so kernel processes can import domain modules
ENV PYTHONPATH="/app:${PYTHONPATH}"
ENV HOME=/home/appuser

# Environment variables for Entra ID (override at runtime)
ENV ENTRA_CLIENT_ID=""
ENV ENTRA_TENANT_ID=""

# Switch to non-root user for runtime
USER appuser

# Expose port for HTTP/SSE server
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1
