# Start the chemistry MCP server

All connection tutorials use the bundled chemistry server as the local
Agora Workbench MCP server. Follow these steps once before running any
tutorial.

## 1. Build the shared MCP base image (once)

From the repository root:

```bash
docker build -f deployment/base.Dockerfile -t mcp-server-base:local .
```

## 2. Bring the server up

> **⚠️ Local-dev auth only** — the bundled server accepts any bearer
> token and binds to `127.0.0.1` only. Do not deploy this configuration.

```bash
cd examples/domain_examples/chemistry
docker compose up -d --build
curl http://localhost:8020/health
# => {"status":"healthy", ...}
```

The server is now listening at `http://localhost:8020/mcp`.

## Cleanup

```bash
cd examples/domain_examples/chemistry && docker compose down
```
