# Debian-based base image for CodeExecutionServer deployments.
#
# Mirrors deployment/base.Dockerfile but on Debian instead of Azure Linux.
# R-language domains (e.g. examples/domain_examples/rstats) require this base:
# on the Azure Linux base, conda-forge R's subprocess spawn (Sys.which during
# the `utils` package's startup) returns ENOSYS, so IRkernel cannot load.
# conda-forge R works correctly on Debian. Python domains can use either base.
#
# Build from the repository root:
#   docker build -f deployment/base.debian.Dockerfile -t mcp-server-base-debian:local .

# ============================================================================
# Stage: Base image with common dependencies
# ============================================================================
FROM python:3.12-slim-bookworm AS base

WORKDIR /app

# Install system dependencies (apt is Debian's package manager).
# NOTE: Compiler toolchain is intentionally omitted — current Python deps
# install from manylinux wheels. Domain server images that add packages
# requiring source builds should install build-essential themselves.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    bash \
    curl \
    git \
    tar \
    gzip \
    bzip2 \
    xz-utils \
    gawk \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user early so subsequent installs can be owned correctly
RUN useradd -m -d /home/appuser -s /bin/bash appuser

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
COPY src/base /app/base
COPY src/code_execution /app/code_execution
COPY src/connector /app/connector

# Set up remaining appuser directories
RUN mkdir -p /home/appuser/.cache/mcp-envs && \
    chown -R appuser:appuser /app /home/appuser

# Add /app to PYTHONPATH so kernel processes can import domain modules
ENV PYTHONPATH="/app"
ENV HOME=/home/appuser

# Switch to non-root user for runtime
USER appuser

# Expose port for HTTP/SSE server
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1
