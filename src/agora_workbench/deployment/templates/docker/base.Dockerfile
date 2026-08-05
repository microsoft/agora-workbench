# Base image for CodeExecutionServer deployments.
#
# Contains system dependencies, uv, miniforge, and the agora-workbench package.
# User server images should extend the locally built or published base image.
#
# Build from your project root (installs agora-workbench from PyPI, so the
# build context does not need a workbench checkout):
#   docker build -f deployment/docker/base.Dockerfile -t mcp-server-base:local .
#
# To pin a specific release:
#   docker build -f deployment/docker/base.Dockerfile \
#       --build-arg AGORA_WORKBENCH_VERSION=0.1.1 -t mcp-server-base:local .
#
# To build against a workbench source checkout instead of the published
# package, run from the workbench repository root with:
#   docker build -f src/agora_workbench/deployment/templates/docker/base.Dockerfile \
#       --build-arg AGORA_WORKBENCH_SOURCE=local -t mcp-server-base:local .
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

# Where agora-workbench comes from: "pypi" (default) or "local" (source checkout).
ARG AGORA_WORKBENCH_SOURCE=pypi
ARG AGORA_WORKBENCH_VERSION=0.1.1

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

# Install uv with version pinning. The uv wheel ships the standalone binary
# itself, so `uv` on PATH is a real executable rather than a Python entry-point
# shim — invoking it does not start an interpreter.
#
# This must stay ahead of the miniforge PATH line below so it installs into the
# system Python and lands in /usr/bin. Installing it after would place uv inside
# the conda base environment, where activating another env drops it off PATH.
ARG UV_VERSION=0.12.2
RUN python3 -m pip install --no-cache-dir --root-user-action=ignore "uv==${UV_VERSION}"

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

# ============================================================================
# Stage: agora-workbench from PyPI (default)
# ============================================================================
# Self-contained — needs nothing from the build context, so this image builds
# from any project root without a workbench checkout present.
FROM base AS workbench-pypi

ARG AGORA_WORKBENCH_VERSION
RUN python3 -m pip install --no-input "agora-workbench==${AGORA_WORKBENCH_VERSION}"

# ============================================================================
# Stage: agora-workbench from a source checkout
# ============================================================================
# Requires the build context to be a workbench repository root. Select with
# --build-arg AGORA_WORKBENCH_SOURCE=local.
FROM base AS workbench-local

# Copy package metadata for dependency resolution, then install runtime deps
COPY pyproject.toml /app/pyproject.toml
RUN python3 -c "import tomllib; deps=tomllib.load(open('/app/pyproject.toml','rb'))['project']['dependencies']; open('/tmp/reqs.txt','w').write('\n'.join(deps))" && \
    python3 -m pip install --no-input -r /tmp/reqs.txt && \
    rm /tmp/reqs.txt

# Copy shared code (used by all servers)
# .dockerignore excludes tests/ and dev files from this COPY
COPY src/agora_workbench /app/agora_workbench

# ============================================================================
# Stage: final image
# ============================================================================
FROM workbench-${AGORA_WORKBENCH_SOURCE} AS final

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
