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

CMD ["python", "-m", "my_server.server"]
