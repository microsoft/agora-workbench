# Start the chemistry MCP server

All connection tutorials use the bundled chemistry server as the local
Agora Workbench MCP server. Follow these steps once before running any
tutorial.

The Agora Workbench library is available from PyPI, but this bundled example and its Docker files are repository
assets. Clone the matching release before continuing:

```bash
git clone --branch v0.1.1 --depth 1 https://github.com/microsoft/agora-workbench.git
cd agora-workbench
```

## 1. Build the shared MCP base image (once)

From the repository root:

```bash
docker build -f src/agora_workbench/deployment/templates/docker/base.Dockerfile -t mcp-server-base:local .
```

## 2. Bring the server up

> **⚠️ Local-dev auth only** — the bundled server accepts any bearer
> token and binds to `127.0.0.1` only. Do not deploy this configuration.

```bash
docker network inspect agora-activity >/dev/null 2>&1 || docker network create agora-activity
cd examples/servers/chemistry
docker compose up -d --build
curl http://localhost:8020/health
# => {"status":"healthy", ...}
```

The server is now listening at `http://localhost:8020/mcp`.

## Cleanup

```bash
cd examples/servers/chemistry && docker compose down
```
