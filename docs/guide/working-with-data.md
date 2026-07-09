# Working with data

Agora Workbench provides several mechanisms for making data available to code execution sessions: asset provisioning for large files, a data catalog for discovery, and asset resolution for seamless parameter injection.

## Asset provisioning

For large files (model weights, reference datasets) that need to be available at server startup, use `AssetSpec` in your `ServerConfig`:

```python
from agora_workbench.code_execution import ServerConfig
from agora_workbench.code_execution.code_execution_models import AssetSpec

config = ServerConfig(
    name="myserver",
    description="...",
    type="uv",
    dependency_file="numpy\npandas\n",
    assets=[
        AssetSpec(
            name="reference-data",
            source="https://storage.blob.core.windows.net/data/reference.parquet",
            destination="data/reference.parquet",
            size_hint_mb=500,
            checksum="sha256:abc123...",
        ),
    ],
    auto_provision=True,
)
```

### Supported source URIs

| Scheme | Example |
|--------|---------|
| `https://` | HTTP download with retry |
| `abfss://` | Azure Blob Storage (ADLS Gen2) |
| `https://*.blob.core.windows.net/` | Azure Blob Storage (classic) |
| `file:///path` or bare path | Local copy (Docker bind mounts) |

### Accessing assets from tool code

Assets are provisioned into the environment cache directory. Tool code accesses them via the `MCP_ASSET_CACHE_DIR` environment variable:

```python
import os
from pathlib import Path

cache = Path(os.environ["MCP_ASSET_CACHE_DIR"])
reference = cache / "data/reference.parquet"
df = pd.read_parquet(reference)
```

!!! note "Provisioning model weights?"
    Assets deliver the **files**. If loading those files (e.g. into a large
    model) is expensive, don't load them inside every session — that pays the
    cost once per kernel. Provision the weights as an asset, then load them once
    in a **[sidecar](sidecars.md)** that serves inference over loopback HTTP.

## Data catalog (DataLakeDataManager)

The `DataLakeDataManager` provides server-side data discovery and caching for dynamic assets — files the agent finds and uses during a session rather than pre-provisioned at startup.

Features:

- **Hybrid search** — keyword (FTS5) and vector (sqlite-vec) search over cataloged assets
- **Automatic caching** — fetched assets are cached to disk; subsequent accesses are instant
- **Multiple backends** — Azure Blob Storage, local filesystem, or custom fetchers

### Asset resolution

When the agent passes asset references in tool parameters (e.g., `grid_file="<blob>abc123</blob>"`), the `AssetResolutionMiddleware` automatically:

1. Detects tagged references in parameter values
2. Fetches and caches the referenced asset
3. Replaces the tag with a local `Path` before the tool receives the parameter

This works transparently — your tool implementation receives a `Path` object, not a blob reference.

### Asset references in code execution

For `execute_{name}_code` calls, asset tags embedded as string literals in the code are detected via AST analysis and resolved automatically:

```python
# The agent writes this code:
data = pd.read_parquet("<blob>abc123</blob>")

# The server resolves it to:
# data = pd.read_parquet(Path("/cache/assets/abc123.parquet"))
```

## Publishing artifacts

Tools and code execution can produce output files. Configure publishers to make these available:

```python
from agora_workbench.code_execution.auth import create_noop_auth_config
from agora_workbench.code_execution.data_access import LocalFilePublisher, BlobPublisher

publishers = [
    LocalFilePublisher(base_dir="/tmp/artifacts"),
    BlobPublisher(
        account_url="https://myaccount.blob.core.windows.net",
        container="outputs",
        credential=create_storage_credential(),
    ),
]

server = CodeExecutionServer(
    server_config=config,
    auth_config=create_noop_auth_config(),
    publishers=publishers,
)
```

The agent publishes artifacts using `<gui>name</gui>` destinations for interactive display, or blob destinations for persistent storage. To minimize unexpected data egress, the publisher tools instruct the agent not to publish files unless instructed by the user.

### Publisher dispatch

Publishers are checked in order via `can_handle()`. The first match wins. A `GuiPublisher` is always prepended automatically, so `<gui>name</gui>` destinations always work.

## Data catalog

The data catalog provides server-side data discovery — the agent can search for files by natural-language query, browse by domain, or run SQL against the catalog metadata. It's backed by SQLite with FTS5 (keyword search) and sqlite-vec (vector similarity).

### Setting up the catalog

Create a `catalog.yaml` file in your server's directory:

```yaml
# catalog.yaml
sources:
  # Local filesystem directory
  - path: /data/weather/
    domain: earthscience
    description: "NOAA daily weather observations for Pacific Northwest"
    files:
      daily_obs.csv:
        description: "Daily temperature and precipitation readings from Pacific NW stations"
      hourly_wind.parquet:
        description: "Hourly wind speed measurements from coastal stations"

  # Another directory with auto-discovered files (no per-file overrides)
  - path: /data/grid/
    domain: powergrid

  # Azure Blob Storage source
  - path: az://mystorageaccount/container/prefix/
    domain: powergrid
    description: "Geospatial transmission line dataset"

  # HTTPS Blob URL format also works
  - path: https://mystorageaccount.blob.core.windows.net/container/prefix/
    domain: powergrid
    files:
      lines.geojson:
        description: "US high-voltage transmission lines with voltage and owner metadata"

search:
  # Only Azure OpenAI embeddings are currently supported for vector search
  embedding_model: azure-openai
  azure_openai_endpoint: https://your-resource.cognitiveservices.azure.com/openai/deployments/text-embedding-3-large/embeddings?api-version=2023-05-15
  azure_openai_deployment: text-embedding-3-large

  # Hybrid ranking weight: 0.0 = pure vector, 1.0 = pure keyword
  hybrid_alpha: 0.5
```

### Source configuration

Each source entry in `sources` declares a data location:

| Field | Required | Description |
|-------|----------|-------------|
| `path` | ✓ | Local path, `az://account/container/prefix/`, or HTTPS blob URL |
| `domain` | | Domain label (used for filtering in search, e.g. `"earthscience"`) |
| `description` | | Default description for files that don't have a per-file override |
| `files` | | Dict of `filename → {description, domain}` overrides |

Source type is inferred automatically from the path:

- Bare paths or `file://` → `local`
- `az://` or `https://*.blob.core.windows.net/` → `blob`

### Search configuration

| Field | Default | Description |
|-------|---------|-------------|
| `embedding_model` | `"azure-openai"` | Embedding provider (only `"azure-openai"` is currently supported) |
| `azure_openai_endpoint` | — | Azure OpenAI embeddings endpoint URL |
| `azure_openai_deployment` | — | Deployment name (e.g. `text-embedding-3-large`) |
| `hybrid_alpha` | `0.5` | Blend weight: 0.0 = pure vector search, 1.0 = pure keyword search |

!!! note "Keyword-only search"
    If you don't have an Azure OpenAI embeddings endpoint, you can still use the catalog with keyword search (FTS5) by omitting the `search` section entirely. The `search_data` tool will use FTS5-only ranking.

### How indexing works

At server startup, the catalog indexer:

1. Reads `catalog.yaml` and discovers files from each source (local directory listing or blob enumeration)
2. Computes a stable artifact ID for each file based on its storage URI
3. Inserts metadata into the SQLite `artifacts` table (with FTS5 triggers for keyword indexing)
4. Computes embeddings in batches and stores them in the sqlite-vec virtual table for vector search

The catalog is stored as a SQLite database on disk, so it persists across server restarts. Re-indexing only adds new files — existing entries are skipped.

### MCP tools exposed

Once configured, the catalog registers four tools on your MCP server (these
tool names are **not** prefixed with the server name, and they appear only when
a catalog is configured):

| Tool | Description |
|------|-------------|
| `search_data` | Hybrid keyword + vector search over the catalog. Supports filters by domain and source type. |
| `get_artifact` | Get full metadata for a specific artifact by ID |
| `list_domains` | List all unique domain labels in the catalog |
| `query_catalog` | Run arbitrary read-only SQL against the catalog database |

### Example: agent using search_data

The agent calls `search_data` to find relevant files, then uses the returned metadata to access them:

```python
# Agent finds relevant data
results = search_data(query="wind speed measurements", domain="earthscience", top=5)
# Returns: [{"id": "abc123", "name": "hourly_wind.parquet", "storage_uri": "/data/weather/hourly_wind.parquet", ...}]

# Agent then reads the file in code execution
import pandas as pd
df = pd.read_parquet("/data/weather/hourly_wind.parquet")
```

### Example: agent using query_catalog

For structured queries beyond natural language search:

```python
# Find all parquet files larger than 100MB
results = query_catalog(
    sql="SELECT name, domain, size_bytes FROM artifacts WHERE content_type = 'application/x-parquet' AND size_bytes > 100000000",
    max_rows=50,
)
```

### Catalog database schema

The underlying SQLite database has this structure:

```sql
-- Main table
CREATE TABLE artifacts (
    id TEXT PRIMARY KEY,         -- Deterministic hash of storage_uri
    name TEXT NOT NULL,          -- Filename
    storage_uri TEXT NOT NULL,   -- Full path or blob URI
    description TEXT,            -- Human-readable description
    domain TEXT,                 -- Domain label
    source_type TEXT,            -- 'local' or 'blob'
    content_type TEXT,           -- MIME type (inferred from extension)
    size_bytes INTEGER,          -- File size
    indexed_at TEXT NOT NULL     -- ISO timestamp
);

-- FTS5 virtual table (keyword search)
CREATE VIRTUAL TABLE artifacts_fts USING fts5(name, description, domain);

-- sqlite-vec virtual table (vector search, created by indexer)
```
