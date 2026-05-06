# MCP Server Deployment

Docker-based deployment for `CodeExecutionServer` instances. Provides a shared base image and examples for local development and Azure Container Apps deployment.

## Quick Start

### 1. Build the base image

From the repo's `src/` directory:

```bash
cd src
docker build -f deployment/mcp_server/base.Dockerfile -t mcp-server-base:local .
```

The base image includes system dependencies, `uv`, miniforge, and the `code_execution` package.

### 2. Create your server Dockerfile

Your Dockerfile extends the base image with your server code:

```dockerfile
FROM mcp-server-base:local

COPY --chown=appuser:appuser my_server/ /app/my_server/
CMD ["python", "-m", "my_server.server"]
```

See [`example/Dockerfile`](example/Dockerfile) for a full example with comments on adding pip dependencies and system packages.

### 3. Run locally with Docker Compose

See [`example/docker-compose.yml`](example/docker-compose.yml) for a ready-to-use compose file:

```bash
docker compose up --build
```

### 4. Deploy to Azure Container Apps

See [`container_apps/README.md`](container_apps/README.md) for infrastructure setup. Quick start:

```bash
cd container_apps
./deploy.sh \
  --server my-server \
  --dockerfile /path/to/your/Dockerfile \
  --context /path/to/build/context
```

## Authentication

The server supports two auth modes:

- **NoOp (local development)** — No env vars needed. Configure your server with
  `create_noop_auth_config()`. Any bearer token (or none) is accepted without validation.
- **Entra ID (production)** — Pass `ENTRA_CLIENT_ID` and `ENTRA_TENANT_ID` at runtime
  via your `.env` file or docker-compose environment. Configure your server with
  `create_entra_auth_config()`.

## Environment Variables

Containers read configuration from a `.env` file. Key variables:

- `ENTRA_CLIENT_ID` / `ENTRA_TENANT_ID` — Entra ID app registration (required only for Entra auth)

See `.env.example` at the repo root for the full list.

## Architecture

```
src/deployment/mcp_server/
├── base.Dockerfile          # Shared base image (system deps, uv, miniforge, code_execution)
├── README.md                # This file
├── example/
│   ├── Dockerfile           # Example server Dockerfile
│   └── docker-compose.yml   # Example local compose setup
└── container_apps/
    ├── deploy.sh            # Build, push, and deploy to Azure Container Apps
    ├── main.bicep           # ARM template for the Container App
    ├── README.md            # Container Apps setup guide
    └── parameters/          # Per-server Bicep parameter files
```

## Build Context

The base image must be built with `src/` as the Docker build context so the Dockerfile can copy `code_execution/` and `middleware/` packages. Rebuild the base image after changes to these packages.
