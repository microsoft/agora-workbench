# Domain Servers

Reference implementations of domain-specific MCP code execution servers built with Agora Workbench.

Each server subclasses `CodeExecutionServer`, ships a containerized Python environment with domain packages pre-installed, and registers domain tools via a `ToolRegistry`.

## Servers

| Server | Domain | Key Packages | Port |
|--------|--------|--------------|------|
| [`chemistry/`](chemistry/) | Cheminformatics | RDKit, scikit-learn | 8020 |
| [`earthscience/`](earthscience/) | Geospatial / remote sensing | rasterio, xarray, pystac-client, Planetary Computer | 8021 |
| [`energysystems/`](energysystems/) | Power system modeling | PyPSA, HiGHS, networkx | 8022 |

## Running a Server Locally

All servers follow the same two-step pattern:

```bash
# 1. Build the shared base image (one-time, from repo root)
docker build -f src/agora_workbench/deployment/templates/docker/base.Dockerfile -t mcp-server-base:local .

# 2. Build and start the server
cd examples/servers/<name>
docker compose up --build
```

See each server's README for verification steps and usage examples.

## Other Directories

| Directory | Purpose |
|-----------|---------|
| `deployment/` | Azure Bicep parameters and multi-server network compose files |
| `tests/` | Integration tests for domain server logic (run with `pytest -m "not live"`) |
