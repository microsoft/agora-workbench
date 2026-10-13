# Example Dockerfile for a CodeExecutionServer.
#
# Prerequisites — build the base image first (from your project root):
#   docker build -f deployment/docker/base.Dockerfile -t mcp-server-base:local .
#
# Then build your server image:
#   docker build -t my-server:local .

FROM mcp-server-base:local

# Copy your server code into the image
COPY --chown=appuser:appuser my_server/ /app/my_server/

# If your server has additional pip dependencies, add them here:
# COPY --chown=appuser:appuser requirements.txt /app/server-requirements.txt
# USER root
# RUN pip install --no-input -r /app/server-requirements.txt
# USER appuser

# If your server needs system packages (e.g. for native libraries):
# USER root
# RUN apt-get update && apt-get install -y --no-install-recommends <packages> \
#     && rm -rf /var/lib/apt/lists/*
# USER appuser

# For Azure Container Apps seccomp isolation:
# 1. Set ServerConfig.build_dir under /opt/kernel-envs and asset_cache_dir
#    under /home/appuser/.cache/mcp-assets.
# 2. Warm the environment while this image still runs as appuser.
# 3. Seal the environment before restoring the non-root runtime user:
#
# RUN python -m my_server.server --warm
# USER root
# RUN chown -R root:root /opt/kernel-envs && chmod -R a-w /opt/kernel-envs
# USER appuser

CMD ["python", "-m", "my_server.server"]
