# Docker Configuration

Multi-stage Docker builds for the code execution servers. Each server shares a common base layer and adds only its own code and dependencies.

## Build Stages

| Stage | Domain | Host Port |
|-------|--------|-----------|
| `example-server` | General data science | 8000 |
| `powergrid-server` | Power grid (PyPSA, HiGHS GPU) | 8001 |
| `process-server` | Process simulation (IDAES) | 8002 |
| `foundry-server` | Azure AI Foundry | 8003 |
| `powergrid-server-cpu` | Power grid (CPU-only HiGHS) | 8005 |
| `gis-server` | Geospatial analysis (GIS) | 8006 |
| `office-server` | Office document processing | 8007 |
| `openlca-server` | Life cycle assessment (LCA) | 8008 |
| `vitrimer-tg-sim-server` | Vitrimer Tg simulation | 8010 |
| `vitrimer-vae-server` | Vitrimer VAE polymer design | 8011 |
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

The recommended workflow uses `build.py` to scaffold a new domain and regenerate
the Docker files automatically.

### Option A — Automated scaffolding (recommended)

```bash
# 1. Scaffold the domain (creates domain.yaml + server stub):
uv run python src/code_execution/docker/build.py new <name>

# 2. Edit the generated domain.yaml and implement your server tools.

# 3. Regenerate Dockerfile + docker-compose.yml:
uv run python src/code_execution/docker/build.py generate

# 4. Build and test:
docker compose build <name>-server
docker compose up <name>-server
```

### Option B — Manual addition

If you prefer to add a server manually:

1. **Create `domains/<name>/domain.yaml`** describing the server:

   ```yaml
   name: myserver
   module: domains.myserver.server.myserver_server
   port: 8012
   description: My domain server
   system_packages: []      # apt packages (optional)
   extra_files:
     - states.py            # files to COPY beyond server/
   extra_env: {}
   depends_on: []
   volumes: []
   trusted_hosts: true
   ```

2. **Add a Dockerfile stage** to `Dockerfile` targeting `base`:

   ```dockerfile
   FROM base AS myserver-server
   COPY --chown=appuser:appuser domains/myserver/server /app/domains/myserver/server
   COPY --chown=appuser:appuser domains/myserver/states.py /app/domains/myserver/states.py
   CMD ["python", "-m", "domains.myserver.server.myserver_server"]
   ```

3. **Add a service** in `docker-compose.yml` using the existing anchors:

   ```yaml
   myserver-server:
     build:
       <<: *common-build
       target: myserver-server
     command: ["python", "-m", "domains.myserver.server.myserver_server"]
     ports:
       - "8012:8000"
     volumes:
       - ~/.azure:/root/.azure:rw
     env_file:
       - ../../.env
     environment:
       <<: [*base-env, *trusted-hosts]
     healthcheck:
       <<: *common-healthcheck
   ```

4. **Update the `*trusted-hosts` anchor** (top of `docker-compose.yml`) to include `myserver-server`.

5. **Register the server** in `server_registry.yaml`.

## `build.py` Reference

```
usage: build.py [-h] {generate,new} ...

subcommands:
  generate    Generate Dockerfile and docker-compose.yml from all domain.yaml files
  new         Scaffold a new domain server (creates domain.yaml + server stub)
```

### `generate`

```bash
uv run python src/code_execution/docker/build.py generate [--root ROOT] [--output-dir DIR]
```

Reads every `domains/*/domain.yaml` under `ROOT` (default: `src/`), renders
`domain.Dockerfile.j2` for each, and combines with `base.Dockerfile` to produce
`Dockerfile`.  Also renders `compose-service.j2` for each domain and writes a
new `docker-compose.yml` with YAML anchors.

### `new`

```bash
uv run python src/code_execution/docker/build.py new <name> [--root ROOT]
```

Creates:
- `domains/<name>/domain.yaml` — pre-filled config (edit port + description)
- `domains/<name>/server/<name>_server.py` — `CodeExecutionServer` subclass stub
- `domains/<name>/__init__.py` and `domains/<name>/server/__init__.py`

## Architecture

```
src/code_execution/docker/
├── base.Dockerfile          # Shared base: GPU wheel builder + base image + powergrid (special case)
├── domain.Dockerfile.j2     # Jinja2 template for standard per-domain stages
├── compose-service.j2       # Jinja2 template for docker-compose service entries
├── build.py                 # CLI: `generate` + `new` commands
├── Dockerfile               # Combined output (base + generated domain stages)
├── docker-compose.yml       # Hand-maintained with YAML anchors; regenerate via build.py
└── README.md                # This file

src/domains/<name>/
├── domain.yaml              # Per-domain config consumed by build.py
├── server/                  # Server implementation
│   └── <name>_server.py
└── states.py                # Optional domain state definitions
```

### Special cases

**PowerGrid** (`powergrid-server` / `powergrid-server-cpu`) uses a complex
multi-stage CUDA build to compile a GPU-enabled HiGHS wheel. These stages live
in `base.Dockerfile` rather than being generated from a template.  The
`domains/powergrid/domain.yaml` references a `Dockerfile.fragment` file (managed
by the domain team) so `build.py generate` can include them once the fragment
is in place.

## Build Context

The build context is `../..` (the AgoraAgentMAF root) so the Dockerfile can copy from `code_execution/`, `tools/`, `domains/`, and other top-level packages.
