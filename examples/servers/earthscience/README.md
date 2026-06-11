# Earth Science MCP Server (Planetary Computer)

A minimal domain example for the agora-workbench BYOA pattern: a
code-execution MCP server that ships an **environment** — conda spec,
auto-imported prelude, and skill markdown — and nothing else.

There are no domain-specific wrapper tools. The agent does geospatial
work by writing Python inside the single `execute_earthscience_code`
tool, using the skills under `skills/` as guidance.

This is the canonical "what does the smallest useful BYOA domain look
like?" example.

## What ships

| Piece | Purpose |
|---|---|
| `server/earthscience_server.py` | Subclasses `CodeExecutionServer`, defines the conda env and the import prelude |
| `skills/SKILL.md` | The one skill: how to find and load data from Planetary Computer (signing, collection IDs, STAC query syntax, signed-URL expiry, memory discipline). Stops at the array boundary — raster math is general Python the agent writes directly. |
| `Dockerfile`, `docker-compose.yml` | Local deployment |

## Pre-installed Packages (conda-forge)

| Package | Purpose |
|---------|---------|
| **pystac-client** | Search satellite imagery catalogs via STAC API |
| **planetary-computer** | Sign requests for Planetary Computer data access |
| **rasterio** | Read/write raster data (GeoTIFF, COG) |
| **xarray** | N-dimensional labeled array analysis |
| **rioxarray** | xarray + rasterio integration for geospatial rasters |
| **geopandas** | Vector geometry and spatial joins |
| **shapely** | Geometric operations (buffer, intersect, union) |
| **numpy** | Numerical computing |
| **pandas** | Data manipulation |
| **scipy** | Scientific computing |
| **matplotlib** | Visualization |

The auto-imported prelude (see `server/earthscience_server.py`) brings
the most-used names into scope inside every `execute_earthscience_code`
call — `planetary_computer`, `pystac_client`, `rasterio`, `xr`,
`rioxarray`, `gpd`, `np`, `pd`, `box`, `Point`, `Polygon`.

## Quick Start

### 1. Build the base image (one-time)

```bash
# From the repository root:
docker build -f src/agora_workbench/deployment/templates/docker/base.Dockerfile -t mcp-server-base:local .
```

### 2. Build and run the earth science server

```bash
cd examples/servers/earthscience
docker compose up --build
```

The server will be available at `http://localhost:8021`. The first
startup takes several minutes while the conda environment is built;
subsequent starts are cached.

### 3. Verify

```bash
curl http://localhost:8021/health
```

## How an Agent Uses This Server

1. Read `skills/SKILL.md` to learn the Planetary Computer conventions
   (sign the catalog, bbox is lon/lat, signed URLs expire after ~1 h,
   how to search and load an array).
2. Use the recipe to fetch the array(s) the user's request needs.
3. Write the analysis (index computation, masking, statistics, plots)
   directly in `execute_earthscience_code` — the skill does not cover
   raster math because the LLM handles general scientific Python fine.

There is no state graph, no `query_state_graph`, no `load_skill` tool
involvement on the server side. The skills are markdown the agent reads
through its host; the server only runs the code.

## Data Access

The Planetary Computer STAC API is **free and publicly accessible**. No
API key or account is needed for searching the catalog or downloading
data (throttled for anonymous access). The
`planetary_computer.sign_inplace` modifier signs asset URLs — also free,
no account required.

## Authentication

This example uses `create_noop_auth_config()` (no authentication on the
MCP server itself). The compose file binds to `127.0.0.1` only — the
server must not be reachable off-host while running in no-op auth mode.
For production deployments with Entra ID, see the
[deployment guide](../../../docs/guide/deploying.md).
