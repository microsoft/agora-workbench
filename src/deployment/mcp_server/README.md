# Docker Configuration

Multi-stage Docker builds for code execution servers. All servers share a common base layer (`base`) and add only their own code and dependencies on top.

## Adding a New Server

The recommended workflow uses `build.py` to scaffold a new domain and regenerate the Docker files automatically.

### Option A — Automated scaffolding (recommended)

```bash
# 1. Scaffold the domain (creates domain.yaml + server stub):
uv run python src/deployment/mcp_server/build.py new <name>

# 2. Edit the generated domain.yaml and implement your server tools.

# 3. Regenerate Dockerfile + docker-compose.yml:
uv run python src/deployment/mcp_server/build.py generate

# 4. Build and test:
docker compose build <name>-server
docker compose up <name>-server
```

### Option B — Manual addition

1. **Create `domains/<name>/domain.yaml`** describing the server:

   ```yaml
   name: myserver
   module: domains.myserver.server.myserver_server
   port: 8000
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
       - "8000:8000"
     env_file:
       - ../../.env
     environment:
       <<: [*base-env, *trusted-hosts]
     healthcheck:
       <<: *common-healthcheck
   ```

4. **Update the `*trusted-hosts` anchor** (top of `docker-compose.yml`) to include `myserver-server`.

5. **Register the server** in `server_registry.yaml`.

### Building without docker compose

If you need to invoke `docker build` directly:

```bash
cd src  # build context root

docker build \
  --target myserver-server \
  -f deployment/mcp_server/Dockerfile \
  -t myserver-server:local .
```

## Environment Variables

Containers read from `../../.env` (the repo root). Key variables:

- `ENTRA_CLIENT_ID` / `ENTRA_TENANT_ID` — Entra ID app registration (required for auth)
- `OBO_SIMULATION_MODE` — set `true` for local development to bypass OBO flow

See `.env.example` for the full list.

## `build.py` Reference

```
usage: build.py [-h] {generate,new} ...

subcommands:
  generate    Generate Dockerfile and docker-compose.yml from all domain.yaml files
  new         Scaffold a new domain server (creates domain.yaml + server stub)
```

### `generate`

```bash
uv run python src/deployment/mcp_server/build.py generate [--root ROOT] [--output-dir DIR]
```

Reads every `domains/*/domain.yaml` under `ROOT` (default: `src/`), renders
`domain.Dockerfile.j2` for each, and combines with `base.Dockerfile` to produce
`Dockerfile`. Also renders `compose-service.j2` for each domain and writes a
new `docker-compose.yml` with YAML anchors.

### `new`

```bash
uv run python src/deployment/mcp_server/build.py new <name> [--root ROOT]
```

Creates:
- `domains/<name>/domain.yaml` — pre-filled config (edit port + description)
- `domains/<name>/server/<name>_server.py` — `CodeExecutionServer` subclass stub
- `domains/<name>/__init__.py` and `domains/<name>/server/__init__.py`

## Architecture

```
src/deployment/mcp_server/
├── base.Dockerfile          # Shared base image (system deps, uv, miniforge, code_execution)
├── domain.Dockerfile.j2     # Jinja2 template for standard per-domain stages
├── compose-service.j2       # Jinja2 template for docker-compose service entries
├── build.py                 # CLI: `generate` + `new` commands
├── Dockerfile               # Combined output (base + generated domain stages)
├── docker-compose.yml       # YAML-anchor header; services added by build.py or manually
└── README.md                # This file

src/domains/<name>/          # Per-domain configs (created by `build.py new`)
├── domain.yaml
├── server/
│   └── <name>_server.py
└── states.py
```

## Build Context

The build context is `../..` from `deployment/mcp_server/` (i.e., `src/`) so the Dockerfile can copy from `code_execution/`, `domains/`, and other top-level packages.
