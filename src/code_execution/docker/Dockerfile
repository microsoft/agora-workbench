# Multi-stage Dockerfile for all code execution servers
# Use --target to build specific server images
# Benefits: Single file, efficient caching, selective copying

# ============================================================================
# Stage: Build GPU-enabled highspy wheel from HiGHS source
# Uses MCR image (Microsoft Container Registry) per org policy.
# CUDA 11.8 devel toolkit is included; we add cmake + Python 3.12 for the build.
# NOTE: Python version MUST match what `uv venv` uses at runtime. Despite the base
# image being devcontainers/python:3.11, miniforge (installed later) provides
# Python 3.12 first on PATH, so uv creates cp312 venvs. The wheel ABI tag must match.
# ============================================================================
FROM mcr.microsoft.com/azureml/minimal-ubuntu22.04-py39-cuda11.8-gpu-inference:20250619.v1 AS highspy-gpu-builder

# MCR inference image runs as 'dockeruser'; switch to root for package installs
USER root

# Prevent interactive prompts (e.g., tzdata timezone selection)
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC

# Install Python 3.12 from deadsnakes PPA + cmake + ninja
RUN apt-get update && apt-get install -y --no-install-recommends \
    software-properties-common && \
    add-apt-repository -y ppa:deadsnakes/ppa && \
    apt-get update && apt-get install -y --no-install-recommends \
    python3.12 python3.12-dev python3.12-venv \
    cmake ninja-build \
    && rm -rf /var/lib/apt/lists/*

# Set up Python 3.12 with build dependencies
RUN python3.12 -m ensurepip && \
    python3.12 -m pip install --upgrade pip && \
    python3.12 -m pip install "scikit-build-core>=0.3.3" pybind11 numpy build

# Copy HiGHS source and build GPU-enabled wheel
WORKDIR /build
COPY domains/powergrid/server/external/HiGHS .

# Sanity check: ensure HiGHS submodule is present with expected build metadata
RUN if [ ! -f pyproject.toml ]; then \
        echo "ERROR: HiGHS submodule appears to be missing or incomplete in 'domains/powergrid/server/external/HiGHS'." >&2; \
        echo "Please run 'git submodule update --init --recursive' before building this Docker image." >&2; \
        exit 1; \
    fi
# Build with GPU support: pass CMake args directly via pip to ensure CUPDLP_GPU is set
ENV HIGHS_BUILD_GPU=ON
RUN python3.12 -m pip wheel . --no-build-isolation --wheel-dir /dist \
    --config-settings=cmake.args="-DPYTHON_BUILD_SETUP=ON;-DCUPDLP_GPU=ON"

# Collect CUDA runtime libraries needed at runtime
# Note: CUDA 11.8 does not have libnvJitLink (introduced in CUDA 12)
RUN mkdir -p /cuda-libs && \
    cp -P /usr/local/cuda/lib64/libcudart.so* /cuda-libs/ && \
    cp -P /usr/local/cuda/lib64/libcublas.so* /cuda-libs/ && \
    cp -P /usr/local/cuda/lib64/libcublasLt.so* /cuda-libs/ && \
    cp -P /usr/local/cuda/lib64/libcusparse.so* /cuda-libs/

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

# Install Azure CLI for local authentication flow
RUN curl -sL https://aka.ms/InstallAzureCLIDeb | bash

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

# Install Python dependencies.
# mise comes from the AgoraHub feed (which has the IDDP feed as an upstream
# source). The feed requires auth, so we obtain an Azure AD token via
# `az account get-access-token` and write a temporary ~/.netrc for Basic auth
# (avoids URL-encoding issues with JWT tokens). Credentials are cleaned up after install.
#
# IMPORTANT: This step requires an 'azure-cli' build context pointing to your
# ~/.azure directory. When building outside of docker-compose, pass it explicitly:
#
#   docker build \
#     --build-context azure-cli=$HOME/.azure \
#     -f code_execution/docker/Dockerfile .
#
RUN --mount=type=bind,from=azure-cli,target=/mnt/azure-cli \
    if [ ! -f /mnt/azure-cli/msal_token_cache.json ] && \
       [ ! -f /mnt/azure-cli/azureProfile.json ]; then \
        echo "ERROR: The 'azure-cli' build context does not contain valid Azure CLI credentials." >&2; \
        echo "Make sure you have run 'az login' and are building with:" >&2; \
        echo "  docker build --build-context azure-cli=\$HOME/.azure -f code_execution/docker/Dockerfile ." >&2; \
        echo "Or use docker compose, which configures this automatically:" >&2; \
        echo "  docker compose -f code_execution/docker/docker-compose.yml build" >&2; \
        exit 1; \
    fi && \
    cp -r /mnt/azure-cli /tmp/.azure && \
    FEED_TOKEN=$(AZURE_CONFIG_DIR=/tmp/.azure az account get-access-token \
        --resource 499b84ac-1321-427f-aa17-267ca6975798 \
        --query accessToken -o tsv) && \
    # rm -rf can fail with "Directory not empty" on overlayfs (BuildKit overlay2);
    # find -delete is immune to this kernel-level race, but may still fail transiently;
    # retry deletion a few times and fail the build if cleanup cannot be guaranteed.
    i=0; max_retries=5; \
    while [ "$i" -lt "$max_retries" ]; do \
        if find /tmp/.azure -delete; then \
            break; \
        fi; \
        i=$((i+1)); \
        echo "Warning: attempt $i to delete /tmp/.azure failed; retrying..." >&2; \
        sleep 1; \
    done; \
    if [ "$i" -eq "$max_retries" ]; then \
        echo "ERROR: failed to securely delete /tmp/.azure after $max_retries attempts" >&2; \
        exit 1; \
    fi && \
    printf "machine pkgs.dev.azure.com\nlogin token\npassword %s\n" "$FEED_TOKEN" > /root/.netrc && \
    chmod 600 /root/.netrc && \
    pip install --no-input \
        --index-url "https://pkgs.dev.azure.com/MSR-Agora/AgoraHub/_packaging/agorahub/pypi/simple/" \
        -r /app/requirements.txt && \
    rm -f /root/.netrc

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

# ============================================================================
# Stage: Example server (minimal dependencies)
# ============================================================================
FROM base AS example-server

# Copy example server code
COPY --chown=appuser:appuser domains/example/server /app/domains/example/server
COPY --chown=appuser:appuser domains/example/states.py /app/domains/example/states.py

CMD ["python", "-m", "domains.example.server.example_server"]

# ============================================================================
# Stage: PowerGrid server (GPU-enabled HiGHS)
# ============================================================================
FROM base AS powergrid-server

# Copy CUDA runtime libraries from the GPU builder stage
# These are needed at runtime for GPU-accelerated HiGHS PDLP solver
COPY --from=highspy-gpu-builder /cuda-libs/ /usr/local/cuda/lib64/
ENV LD_LIBRARY_PATH="/usr/local/cuda/lib64:${LD_LIBRARY_PATH}"

# NVIDIA Container Toolkit env vars (ensures GPU access at runtime)
ENV NVIDIA_VISIBLE_DEVICES=all
ENV NVIDIA_DRIVER_CAPABILITIES=compute,utility

# Copy pre-built GPU-enabled highspy wheel to wheelhouse
# This will be preferred over the PyPI CPU-only version via --find-links
COPY --from=highspy-gpu-builder /dist/highspy*.whl /opt/wheelhouse/

# Copy powergrid server including tools package
COPY --chown=appuser:appuser domains/powergrid/server /app/domains/powergrid/server
COPY --chown=appuser:appuser domains/powergrid/states.py /app/domains/powergrid/states.py

CMD ["python", "-m", "domains.powergrid.server.powergrid_server"]

# ============================================================================
# Stage: PowerGrid server (CPU-only, no NVIDIA GPU required)
# ============================================================================
FROM base AS powergrid-server-cpu

# Build steps require root for system-wide pip install
USER root

# Build highspy from the local HiGHS submodule (CPU-only, no CUDA)
WORKDIR /build
COPY domains/powergrid/server/external/HiGHS .
RUN if [ ! -f pyproject.toml ]; then \
        echo "ERROR: HiGHS submodule appears to be missing or incomplete in 'domains/powergrid/server/external/HiGHS'." >&2; \
        echo "Please run 'git submodule update --init --recursive' before building this Docker image." >&2; \
        exit 1; \
    fi
RUN uv pip install --system . && \
    rm -rf /home/appuser/.cache/uv
WORKDIR /app

# Copy powergrid server including tools package
COPY --chown=appuser:appuser domains/powergrid/server /app/domains/powergrid/server
COPY --chown=appuser:appuser domains/powergrid/states.py /app/domains/powergrid/states.py

USER appuser
CMD ["python", "-m", "domains.powergrid.server.powergrid_server"]

# ============================================================================
# Stage: Process server
# ============================================================================
FROM base AS process-server

# Copy process server code
COPY --chown=appuser:appuser domains/process/server /app/domains/process/server
COPY --chown=appuser:appuser domains/process/states.py /app/domains/process/states.py

CMD ["python", "-m", "domains.process.server.process_server"]

# ============================================================================
# Stage: Foundry server (wraps Azure AI Foundry built-in tools as MCP)
# ============================================================================
FROM base AS foundry-server

# Copy Foundry server code
COPY --chown=appuser:appuser domains/foundry/server /app/domains/foundry/server
COPY --chown=appuser:appuser domains/foundry/__init__.py /app/domains/foundry/__init__.py
COPY --chown=appuser:appuser domains/foundry/states.py /app/domains/foundry/states.py

CMD ["python", "-m", "domains.foundry.server.foundry_server"]

# ============================================================================
# Stage: GIS server (geospatial analysis)
# ============================================================================
FROM base AS gis-server

# Copy GIS server code
COPY --chown=appuser:appuser domains/gis/server /app/domains/gis/server
COPY --chown=appuser:appuser domains/gis/__init__.py /app/domains/gis/__init__.py
COPY --chown=appuser:appuser domains/gis/states.py /app/domains/gis/states.py

CMD ["python", "-m", "domains.gis.server.gis_server"]

# ============================================================================
# Stage: Office server (Excel, Word, PowerPoint extraction + IRM decryption)
# ============================================================================
FROM base AS office-server

# Copy office server code
COPY --chown=appuser:appuser domains/office/server /app/domains/office/server
COPY --chown=appuser:appuser domains/office/states.py /app/domains/office/states.py

CMD ["python", "-m", "domains.office.server.office_server"]

# ============================================================================
# Stage: OpenLCA server (pure-Python olca-ipc client; IPC server is a sidecar)
# ============================================================================
FROM base AS openlca-server

# Copy OpenLCA server code (includes the openlca-tools package)
COPY --chown=appuser:appuser domains/openlca/server /app/domains/openlca/server
COPY --chown=appuser:appuser domains/openlca/__init__.py /app/domains/openlca/__init__.py
COPY --chown=appuser:appuser domains/openlca/states.py /app/domains/openlca/states.py

CMD ["python", "-m", "domains.openlca.server.openlca_server"]

# ============================================================================
# Stage: Vitrimer Tg simulation server (EMC + LAMMPS + PCFF)
# ============================================================================
FROM base AS vitrimer-tg-sim-server

USER root

# Install LAMMPS with CLASS2 (PCFF) support
RUN apt-get update && \
    apt-get install -y --no-install-recommends lammps && \
    rm -rf /var/lib/apt/lists/*

USER appuser

# Copy vitrimer_tg_sim server code (includes the tools package)
COPY --chown=appuser:appuser domains/vitrimer_tg_sim/server /app/domains/vitrimer_tg_sim/server

ENV LAMMPS_CMD="lmp"

CMD ["python", "-m", "domains.vitrimer_tg_sim.server.vitrimer_tg_sim_server"]

# ============================================================================
# Stage: Vitrimer VAE server (HierVAE generative model for polymer design)
# ============================================================================
FROM base AS vitrimer-vae-server

# Copy vitrimer_vae server code (tools package + small data files;
# the large model checkpoint is fetched lazily from blob storage at runtime)
COPY --chown=appuser:appuser domains/vitrimer_vae/server /app/domains/vitrimer_vae/server

CMD ["python", "-m", "domains.vitrimer_vae.server.vitrimer_vae_server"]
