# Base image for CodeExecutionServer deployments.
#
# Contains system dependencies, uv, miniforge, and the code_execution package.
# User server images should extend the locally built or published base image.
#
# Build from the repository root:
#   docker build -f deployment/base.Dockerfile -t mcp-server-base:local .
#
# Then create your own Dockerfile:
#   FROM mcp-server-base:local
#   COPY --chown=appuser:appuser my_server/ /app/my_server/
#   RUN python -m my_server.server --warm
#   CMD ["python", "-m", "my_server.server"]
#
# The --warm flag pre-builds the Python environment (conda/pip/uv) during
# docker build, so the container starts immediately at runtime without needing
# network access or large ephemeral storage. The server's _ensure_environment()
# detects the pre-built env and skips building. This step must be in the
# server-specific Dockerfile (not here) because it requires the server code
# to already be COPY'd into the image.

# ============================================================================
# Stage: Base image with common dependencies
# ============================================================================
FROM mcr.microsoft.com/azurelinux/base/python:3.12 AS base

WORKDIR /app

# Create non-root user early so subsequent installs can be owned correctly
RUN tdnf install -y shadow-utils && tdnf clean all && \
    useradd -m -d /home/appuser -s /bin/bash appuser

# Install system dependencies (tdnf is Azure Linux's package manager)
# NOTE: Compiler toolchain (gcc, gcc-c++, make, glibc-devel, python3-devel,
# openssl-devel, libffi-devel, sqlite-devel, pkgconf) is intentionally omitted
# — all current Python deps install from manylinux wheels. Domain server images
# that add packages requiring source builds should install these themselves.
RUN tdnf install -y \
    ca-certificates \
    bash \
    curl \
    git \
    tar \
    gzip \
    bzip2 \
    xz \
    gawk \
    && tdnf clean all

# Install uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh && \
    mv /root/.local/bin/uv /usr/local/bin/uv && \
    mv /root/.local/bin/uvx /usr/local/bin/uvx

# Install mamba with version pinning (owned by appuser to avoid chown layer)
ARG MINIFORGE_VERSION=25.11.0-1
RUN curl -L -O "https://github.com/conda-forge/miniforge/releases/download/${MINIFORGE_VERSION}/Miniforge3-Linux-x86_64.sh" && \
    bash Miniforge3-Linux-x86_64.sh -b -p /opt/miniforge3 && \
    rm Miniforge3-Linux-x86_64.sh && \
    chown -R appuser:appuser /opt/miniforge3
ENV PATH="/opt/miniforge3/bin:$PATH"

# Ensure pip is up to date
RUN python3 -m pip install --upgrade pip

# Pre-download wheels for Jupyter kernel stack to speed up runtime env builds.
# The CodeExecutionServer registers an ipykernel, and ipykernel pulls in IPython.
# Having these wheels locally avoids repeated downloads during environment creation.
RUN mkdir -p /opt/wheelhouse && \
    python3 -m pip download --dest /opt/wheelhouse \
    "ipykernel>=6.29.0" \
    && chown -R appuser:appuser /opt/wheelhouse

# Copy package metadata for dependency resolution, then install runtime deps
COPY pyproject.toml /app/pyproject.toml
RUN python3 -c "import tomllib; deps=tomllib.load(open('/app/pyproject.toml','rb'))['project']['dependencies']; open('/tmp/reqs.txt','w').write('\n'.join(deps))" && \
    python3 -m pip install --no-input -r /tmp/reqs.txt && \
    rm /tmp/reqs.txt

# Copy shared code (used by all servers)
# .dockerignore excludes tests/ and dev files from this COPY
# base/ holds BaseMCPServer, imported by code_execution/server.py as `from base import ...`;
# it must be on /app (PYTHONPATH) alongside code_execution.
COPY src/base /app/base
COPY src/code_execution /app/code_execution
COPY src/base /app/base

# Set up remaining appuser directories (no large chown needed — miniforge
# and wheelhouse were already owned correctly in their install layers)
RUN mkdir -p /home/appuser/.cache/mcp-envs && \
    chown -R appuser:appuser /app /home/appuser

# Add /app to PYTHONPATH so kernel processes can import domain modules
ENV PYTHONPATH="/app"
ENV HOME=/home/appuser

# Authentication: pass ENTRA_CLIENT_ID and ENTRA_TENANT_ID at runtime
# for production (Entra ID). For local development, configure your server
# with create_noop_auth_config() and no env vars are needed.

# Switch to non-root user for runtime
USER appuser

# Expose port for HTTP/SSE server
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1
