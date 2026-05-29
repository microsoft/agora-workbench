# Activity UI sidecar — receives events from MCP servers and serves the UI.
#
# Build from src/:
#   cd src
#   docker build -f activity_ui/Dockerfile -t activity-ui:local .

FROM mcr.microsoft.com/azurelinux/base/python:3.12

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY activity_ui/requirements.txt /app/requirements.txt
RUN pip install --no-input -r /app/requirements.txt

COPY activity_ui /app/activity_ui

EXPOSE 8030

CMD ["python", "-m", "activity_ui.server"]
