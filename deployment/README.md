# MCP Server Deployment

Docker-based deployment for `CodeExecutionServer` instances. Provides a shared base image and examples for local development and Azure Container Apps deployment.

## Quick Start

### 1. Build the base image

From the repository root:

```bash
# From the repository root:
docker build -f deployment/base.Dockerfile -t mcp-server-base:local .
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

Connector network example (connector + two upstream domain servers):

```bash
docker compose -f deployment/example/docker-compose.network.yml up --build
```

### 4. Deploy to Azure Container Apps

See [`container_apps/README.md`](container_apps/README.md) for infrastructure setup. Quick start:

```bash
cd container_apps
./deploy.sh \
  --server my-server \
  --dockerfile /path/to/your/Dockerfile \
  --context /path/to/build/context

# Or deploy a full connector topology
./deploy.sh --network networks/science-hub.yaml
```

## Authentication

The server supports two auth modes:

- **NoOp (local development)** — No env vars needed. Configure your server with
  `create_noop_auth_config()`. Any bearer token (or none) is accepted without validation.
- **Entra ID (production)** — Pass `ENTRA_CLIENT_ID` and `ENTRA_TENANT_ID` at runtime
  via your `.env.server` file or docker-compose environment. Configure your server with
  `create_entra_auth_config()`.

For connector networks, treat the connector as the external auth boundary.
Upstream domain servers should validate connector service-to-service identity tokens
or share tenant/audience settings for end-user token pass-through.

## Environment Variables

Containers read configuration from a `.env.server` file. Key variables:

- `ENTRA_CLIENT_ID` / `ENTRA_TENANT_ID` — Entra ID app registration (required only for Entra auth)

See `.env.example` at the repo root for the full list.

## Architecture

```
deployment/
├── base.Dockerfile          # Shared base image (system deps, uv, miniforge, code_execution)
├── README.md                # This file
├── example/
│   ├── Dockerfile           # Example server Dockerfile
│   ├── docker-compose.yml   # Example local compose setup
│   └── docker-compose.network.yml  # Connector + upstream network example
└── container_apps/
    ├── deploy.sh            # Build, push, and deploy to Azure Container Apps
    ├── main.bicep           # ARM template for the Container App
    ├── README.md            # Container Apps setup guide
    ├── networks/            # Network manifest examples for ordered deployment
    └── parameters/          # Per-server Bicep parameter files (includes connector template)
```

## Build Context

The base image must be built with `src/` as the Docker build context so the Dockerfile can copy the `code_execution/` package. Rebuild the base image after changes to this package.

## Environment Caching & Large Assets

By default, `CodeExecutionServer` builds its Python environment on first startup
(`auto_build=True`). For servers with large model weights or data files, you can
also declare **assets** that are provisioned into the cache directory.

### Deployment strategies

| Strategy | Best for | How |
|----------|----------|-----|
| **Auto-build to volume** (recommended) | Most servers | Mount a named volume at `/home/appuser/.cache/mcp-envs`. First start is slow; restarts are instant. |
| **Pre-warm then run** | Large assets, predictable cold-start | Run `docker compose --profile setup run --rm warm-cache` before `docker compose up`. |
| **Bake into image** | Air-gapped / minimal cold-start | Add `RUN` steps in your Dockerfile to build the env and download assets at image build time. |
| **Azure Files (ACA)** | Container Apps production | Mount an Azure File Share so the cache persists across replicas. See [`container_apps/README.md`](container_apps/README.md#environment-caching-with-azure-files). |

### Pre-warm CLI

The `agora_workbench.code_execution.cli warm` command builds the environment and provisions
assets without starting the HTTP server:

```bash
# Locally
python -m agora_workbench.code_execution.cli warm --config config.yaml -v

# In Docker (via compose profile)
docker compose --profile setup run --rm warm-cache
```

### Asset configuration

Add `assets` to your `ServerConfig` to declare large files:

```python
ServerConfig(
    name="my-model",
    description="Server with large ML model",
    type="uv",
    dependency_file=requirements_txt,
    assets=[
        AssetSpec(
            name="model-weights",
            source="az://models/v2/weights.safetensors",
            destination="models/weights.safetensors",
            size_hint_mb=2200,
            checksum="abc123...",
        ),
        AssetSpec(
            name="tokenizer",
            source="file:///mnt/shared/tokenizer.json",
            destination="models/tokenizer.json",
        ),
    ],
)
```

Supported source schemes: `https://`, `az://<container>/<blob>`, `file://`, or bare local paths.
