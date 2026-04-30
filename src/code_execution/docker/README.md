# Docker Configuration

Multi-stage Docker builds for the code execution servers. Each server shares a common base layer and adds only its own code and dependencies.

## Build Stages

| Stage | Domain | Host Port |
|-------|--------|-----------|
| `example-server` | General data science | 8000 |
| `powergrid-server` | Power grid (PyPSA, HiGHS GPU) | 8001 |
| `process-server` | Process simulation (IDAES) | 8002 |
| `foundry-server` | Azure AI Foundry | 8003 |
| `dwsim-server` | DWSIM chemical simulation | 8004 |
| `gis-server` | Geospatial analysis (GIS) | 8006 |
| `office-server` | Office document processing | 8007 |
| `openlca-server` | Life cycle assessment (LCA) | 8008 |
| `openlca-ipc` *(sidecar)* | openLCA IPC JSON-RPC server | 8080 |

The `openlca-ipc` sidecar service (port 8080) is also defined in `docker-compose.yml`. It is a custom-built image (see `domains/openlca/docker/`) that runs the openLCA IPC server alongside `openlca-server`.

## Prerequisites: Azure CLI Authentication

The Docker build installs the `mise` package from a private Azure DevOps Artifacts feed (`agorahub`). This requires valid Azure CLI credentials at build time.

**Before building, log in with the Azure CLI:**

```bash
az login
```

The `docker-compose.yml` automatically mounts `~/.azure` as an additional build context so the Dockerfile can obtain a short-lived feed token via `az account get-access-token`. If credentials are absent or expired the build will fail with an explicit error message.

> **Note:** The `~/.azure` directory is only used during the build step to fetch the feed token — it is never baked into the final image.

## Usage

```bash
cd code_execution/docker

# Build and run a single server (recommended: use docker compose so ~/.azure is wired up automatically)
docker compose up example-server --build

# Build and run all servers
docker compose up --build

# Health check
curl http://localhost:8000/health
```

### Building without docker compose

If you need to invoke `docker build` directly you must supply the `azure-cli` build context manually:

```bash
cd AgoraAgentMAF  # build context root

docker build \
  --build-context azure-cli=$HOME/.azure \
  --target example-server \
  -f code_execution/docker/Dockerfile \
  -t example-server:local .
```

## Environment Variables

Containers read from `../../.env` (the AgoraAgentMAF root). Key variables:

- `ENTRA_CLIENT_ID` / `ENTRA_TENANT_ID` — Entra ID app registration (required for auth)
- `OBO_SIMULATION_MODE` — set `true` for local development to bypass OBO flow

See `.env.example` for the full list.

## OpenLCA IPC Sidecar

The `openlca-server` depends on `openlca-ipc`, a custom Java-based sidecar that exposes the openLCA JSON-RPC API on port 8080. Its Dockerfile lives at `domains/openlca/docker/Dockerfile` and uses MCR base images (`mcr.microsoft.com/openjdk/jdk:17-ubuntu`); it pulls the `olca-ipc` library JARs from Maven Central at build time — no external registry images are used.

### Database setup

The `openlca-ipc` container serves databases from the `openlca-data` Docker named volume, mounted at `/app/data` inside the container. The volume is declared in `docker-compose.yml` and created automatically by Docker Compose, but it starts **empty**. To load an openLCA database:

```bash
# 1. Start the services once so Docker Compose creates the volume, then stop:
docker compose up openlca-ipc --no-start

# 2. Populate the volume with your openLCA databases (adjust the source path):
docker run --rm \
  -v openlca-data:/dst \
  -v $HOME/openLCA-data-1.4:/src:ro \
  ubuntu:24.04 \
  cp -a /src/. /dst/

# 3. Start the services (the IPC server will find the databases on startup):
docker compose up openlca-ipc openlca-server --build
```

The `openlca-ipc` server accepts extra arguments (appended to the `docker run` or compose `command:`) such as `-db <name>` to pre-select a database and `--readonly` to forbid writes. Example:

```yaml
# in docker-compose.yml, override the command for read-only mode:
command: ["-db", "mydb", "--readonly"]
```

## Adding a New Server

1. Create a Dockerfile stage targeting `base`:

   ```dockerfile
   FROM base AS myserver-server
   COPY domains/myserver /app/domains/myserver
   CMD ["python", "-m", "domains.myserver.server.myserver_server"]
   ```

2. Add a service in `docker-compose.yml` following the existing pattern (include `additional_contexts: azure-cli: ~/.azure` under `build`).

3. Register the server in `server_registry.yaml`.

## Build Context

The build context is `../..` (the AgoraAgentMAF root) so the Dockerfile can copy from `code_execution/`, `tools/`, `domains/`, and other top-level packages.
