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

### Custom artifact resolution

The `<blob>id</blob>` payload is an opaque catalog identifier. By default it is looked up in an Azure AI Search index (`DATA_LAKE_SEARCH_ENDPOINT` / `DATA_LAKE_BLOB_DETAILS_INDEX`). Deployments whose catalog is a manifest file, a database, a REST service, or an offline test fixture can supply their own resolver instead:

```python
from agora_workbench.data_lake.execution import DataLakeDataManager


class ManifestArtifactResolver:
    """Resolve artifact ids from a preloaded manifest."""

    def __init__(self, manifest: dict[str, str]):
        self._manifest = manifest

    async def resolve(self, artifact_id: str) -> str:
        if artifact_id not in self._manifest:
            raise ValueError(f"Unknown artifact: {artifact_id}")
        return self._manifest[artifact_id]

    @property
    def unavailable_reason(self) -> str | None:
        return None if self._manifest else "The asset manifest is empty."


manifest = {
    "abc123": "https://acct.blob.core.windows.net/datasets/hourly_wind.parquet",
    "def456": "/mnt/data/reference/grid_topology.json",
}

manager = DataLakeDataManager(artifact_resolver=ManifestArtifactResolver(manifest))
```

`resolve` returns any qualified name a registered fetcher can handle (`https://`, `abfss://`, `az://`, or a local path). `unavailable_reason` returns `None` when resolution is ready, or a short operator-facing explanation otherwise — it is folded into the asset-tag guidance the agent sees, so the guidance stays truthful for whichever backend is in use.

The `ArtifactResolver` protocol is available from `agora_workbench.data_lake`
for type annotations. It is structural, so a resolver does not need to subclass
it.

The manager calls `resolve` on each manager cache miss; resolver implementations
own any backend-result caching. Resolvers may also define
`async def aclose(self)` to release backend clients, and the manager calls it
during cleanup when present. A resolver you supply is used as-is and is not
handed the manager's Azure credential, so it must arrange its own authentication.
It may close clients it creates, but must not close credentials or other resources
borrowed from its caller.

If you omit `artifact_resolver`, the manager uses the built-in Azure AI Search
resolver.

## Publishing artifacts

Tools and code execution can produce output files. Configure publishers to make these available:

```python
from agora_workbench.code_execution.auth import create_noop_auth_config
from agora_workbench.data_lake.execution import BlobPublisher, LocalFilePublisher, create_storage_credential

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

The data catalog provides server-side data discovery — the agent can search for files by natural-language query, browse by domain, or run SQL against the catalog metadata. SQLite FTS5 keyword search is always available; sqlite-vec vector similarity is loaded only when vector search is selected.

### Catalog installation options

| Installation | Catalog capabilities |
|--------------|----------------------|
| `agora-workbench` | Local sources and FTS5 keyword search; no Azure SDK, OpenAI client, or sqlite-vec installation |
| `agora-workbench[azure]` | Azure Blob sources, Entra credentials, and Azure AI Search, still without vector dependencies |
| `agora-workbench[catalog-vector]` | sqlite-vec and the OpenAI client; use an injected credential provider for Azure OpenAI |
| `agora-workbench[azure,catalog-vector]` | Built-in Azure sources and Azure OpenAI hybrid search |

These are installation boundaries, not configuration modes. Installing the
`azure` extra does not contact Azure or require credentials during a local
keyword-only catalog import. A base-only installation is different: the cloud
SDKs are absent, and selecting a cloud capability reports that the `azure`
extra is required.

The base distribution still includes the existing MCP, execution, session, and
kernel runtime dependencies. Importing the backend-neutral
`agora_workbench.data_lake` contracts does not initialize execution or cloud
modules. Import concrete catalog, resolver, and execution implementations from
their documented public submodules; optional extras determine whether cloud
and vector backends are available.

### Setting up the catalog

Create a `catalog.yaml` file in your server's directory:

```yaml
# catalog.yaml
version: 1
sources:
  # Scan mode is the compatibility default. Every discovered non-hidden file
  # under the validated root is disclosed.
  - source_id: weather
    path: /data/weather/
    discovery: scan
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

  # Manifest mode is authoritative. Only manifest entries are disclosed.
  - source_id: approved-model-inputs
    path: az://mystorageaccount/approved/model-inputs/
    discovery: manifest
    manifest: catalog.manifest.json
    max_stale_seconds: 300

search:
  # Only Azure OpenAI embeddings are currently supported for vector search
  embedding_model: azure-openai
  azure_openai_endpoint: https://your-resource.openai.azure.com
  azure_openai_deployment: text-embedding-3-large
  # Optional: request a supported shortened vector size. Omit this for the
  # deployment's service-default dimensions.
  # embedding_dimensions: 1536

  # Hybrid ranking weight: 0.0 = pure vector, 1.0 = pure keyword
  hybrid_alpha: 0.5
```

Set `source_id` explicitly when artifact identity must remain stable after moving
a local root. See
[Identity, revisions, and compatibility](data-lake-api.md#identity-revisions-and-compatibility)
for the full contract.

Construct the database from the same search configuration so an explicit
dimension is applied consistently. When `embedding_dimensions` is omitted,
`CatalogDB` infers the service-default size from the first embedding and, on
reopen, from the existing vector table:

```python
from agora_workbench.data_lake.catalog import CatalogConfig, CatalogDB

config = CatalogConfig.from_yaml("catalog.yaml")
catalog = CatalogDB(
    "catalog.db",
    vec_dimensions=config.search.embedding_dimensions,
)
catalog.open()
```

### Mounting a catalog on `CodeExecutionServer`

Catalogs are opt-in. The supported host composition point is
`CatalogIntegration`, passed to `CodeExecutionServer`:

```python
from agora_workbench.code_execution import CatalogIntegration, CodeExecutionServer
from agora_workbench.data_lake import CatalogPolicyMode, DevelopmentAllowAllCatalogAuthorizer
from agora_workbench.data_lake.catalog import CatalogConfig

catalog_config = CatalogConfig.from_yaml("catalog.yaml")
catalog = CatalogIntegration.from_config(
    catalog_config,
    authorizer=DevelopmentAllowAllCatalogAuthorizer(),  # development only
    policy_mode=CatalogPolicyMode.HOMOGENEOUS_SOURCE,
)

server = CodeExecutionServer(
    server_config=config,
    auth_config=auth,
    catalog=catalog,
)
```

The server loads configured scan or manifest sources during startup, fails
startup if no ready generation is available, and closes its owned catalog on
startup rollback or shutdown. `search_data`, `get_artifact`, `list_domains`,
and `get_catalog_capabilities` are registered only when `catalog` is supplied.
Search/get payloads retain `id`, `source_id`, familiar metadata fields, and
relevance `score` when supplied by the provider. They do not eagerly resolve or
expose credential-bearing storage locators. A `load_path` is returned only when
the session uses the integration-provided resolver; paste that opaque,
revision-pinned tag into `execute_*_code`, where resolution occurs on demand.

Do not also call the legacy `register_catalog_tools()` on the same MCP server:
both surfaces own `search_data`, `get_artifact`, and `list_domains`, so
registration fails explicitly rather than silently replacing handlers. The
policy-aware integration deliberately does not expose legacy `query_catalog`;
raw SQL remains available only through `register_catalog_admin_tools()` on a
separately authorized administrative MCP surface.

For production, provide an application authorizer or `authorizer_factory`.
The factory receives a `SessionContext`; each execution session gets a distinct
policy wrapper, immutable request context, resolver, and data-manager cache.
Token claims are available to policy as `context.attributes["claims"]`, but
bearer tokens are not copied into catalog request attributes.

If the supplied `SessionManager` already has a `data_manager_factory`, the
server preserves that manager and its resolver. Discovery remains available,
but catalog results omit `load_path` because the server cannot assume a custom
resolver understands its opaque references. Applications that need both should
compose the catalog resolver in their custom manager factory.

To mount an application-managed provider, make ownership explicit:

```python
from agora_workbench.data_lake import ResourceLease, ResourceOwnership

catalog = CatalogIntegration(
    ResourceLease(provider, ResourceOwnership.BORROWED),
    authorizer_factory=make_authorizer,
)
```

Borrowed providers are neither loaded nor closed by the server by default.
Owned providers are loaded at startup and closed at shutdown. Override
`load_on_startup` only when the application has a different refresh owner.
Catalog refresh remains an administrative/application operation; no agent
reindex or filesystem-watcher tool is registered.

`capability_extension_factory` is a narrow session-scoped composition seam for
applications that add separately authorized capabilities. It is intentionally
writer-neutral today; a managed writer can use the seam after its write
operations are added to the public `CatalogOperation` contract. The factory
receives the `SessionContext`, authorized read catalog, and immutable
`RequestContext`. Returned extension objects may provide
`capabilities(request_context)`; those source capabilities are merged into
`get_catalog_capabilities`, and the extension is closed with the session. The
read provider remains independently owned and is not treated as a writer.
Extensions may implement async-only `aclose()`. Synchronous session closure and
timeout cleanup schedule and retain that work; server shutdown waits for it
before closing the shared provider. Cleanup attempts the manager, every
extension, session payload, and session files independently, reporting
aggregated failures only after all steps have run.

### Source configuration

Each source entry in `sources` declares a data location:

| Field | Required | Description |
|-------|----------|-------------|
| `path` | ✓ | Local path, `az://account/container/prefix/`, or HTTPS blob URL |
| `source_id` | manifest mode | Stable source identity. Strongly recommended in scan mode and required in manifest mode |
| `discovery` | | `scan` (default) or authoritative `manifest` |
| `manifest` | manifest mode | Local path within the source root, or Blob name/full URI within the source prefix |
| `max_stale_seconds` | | How long a previously valid manifest generation may remain readable after refresh failure |
| `domain` | | Domain label (used for filtering in search, e.g. `"earthscience"`) |
| `description` | | Default description for files that don't have a per-file override |
| `files` | | Dict of `filename → {description, domain, artifact_id, aliases}` metadata overrides |

Source type is inferred automatically from the path:

- Bare paths or `file://` → `local`
- `az://` or `https://*.blob.core.windows.net/` → `blob`

`files` remains a metadata override map. It is never an allowlist. In scan mode,
files not named in `files` are still discovered. In manifest mode, registration
comes only from the manifest; a `files` entry can override metadata for a
registered path but cannot register an otherwise absent file.

### Authoritative manifests

Manifest files use strict JSON with this versioned shape:

```json
{
  "version": 1,
  "generation": 42,
  "artifacts": [
    {
      "path": "approved/data.csv",
      "artifact_id": "approved-data",
      "name": "data.csv",
      "description": "Approved experiment input",
      "domain": "science",
      "media_type": "text/csv",
      "size_bytes": 1204,
      "content_revision": "sha256:4f...",
      "metadata_revision": "metadata-7",
      "checksum_sha256": "4f...",
      "aliases": ["external:experiment-input"]
    }
  ]
}
```

`generation` is a positive, monotonically non-decreasing integer. Reusing a
generation with different manifest bytes is rejected, as is rolling back to an
older generation. Artifact paths are normalized source-relative paths and must
remain inside the configured local root or Blob prefix. Duplicate paths,
artifact IDs, storage locators, or source-scoped aliases; duplicate JSON keys;
unknown fields; oversized or malformed JSON; missing manifests; and unsupported
versions fail the source refresh explicitly. Manifest JSON is size- and
item-bounded and does not support YAML anchors or aliases. The 4 MiB byte limit
is enforced while reading: local reads stop at limit-plus-one bytes, and Blob
downloads request and consume only a bounded range of response chunks.

Manifest mode never falls back to listing the source. A valid empty manifest is
an authoritatively empty catalog and tombstones previously registered entries.
An invalid or unavailable manifest preserves the last valid SQLite generation
according to the configured stale-read bound; if no valid generation has ever
loaded, the provider is not ready and all operations fail closed. Fixing the
manifest or credentials and retrying `load()` on the same provider is supported.
Public load/readiness errors contain source IDs and generic categories, not
absolute local paths, SAS query values, or backend response details.

Local and Blob manifests use the same records and identity rules. Blob loading
downloads the configured manifest object directly and records the ETag from that
same download response; it does not enumerate the container or issue a second
properties request. If the response exposes no ETag, the SHA-256 digest of the
downloaded bytes is used as the generation token. Azure SDK imports remain
optional and selecting a Blob source without `agora-workbench[azure]` fails with
the existing explicit optional-dependency error.

### Conversion and dry-run

Existing unversioned `catalog.yaml` files remain valid and are interpreted as
version 1 with `discovery: scan`. Convert one to an explicit representation
without touching storage:

```python
from agora_workbench.data_lake.catalog import convert_catalog_config

report = convert_catalog_config("catalog.yaml")  # dry-run by default
print(report.rendered_yaml)
print(report.summary)
```

Pass a separate destination and `dry_run=False` to write the converted file.
The conversion report distinguishes `configuration_valid`,
`manifest_checked`, and `manifest_content_valid`; side-effect-free conversion
does not claim that an unaccessed manifest is valid.
`CatalogIndexer.dry_run()` performs storage-backed enumeration/manifest
validation and reports additions, updates, deletions, unchanged entries,
generation/ETag, whether the manifest was checked, manifest content validity,
and errors without writing SQLite or computing embeddings.

### Search configuration

| Field | Default | Description |
|-------|---------|-------------|
| `embedding_model` | `"none"` | `"none"` for keyword-only search or `"azure-openai"` for vector search |
| `azure_openai_endpoint` | — | Azure OpenAI resource endpoint, such as `https://my-resource.openai.azure.com` |
| `azure_openai_deployment` | — | Deployment name (e.g. `text-embedding-3-large`) |
| `embedding_dimensions` | service default | Optional requested vector size. Set only when the deployed model supports shortening; pass the same value to `CatalogDB` |
| `hybrid_alpha` | `0.5` | Blend weight: 0.0 = pure vector search, 1.0 = pure keyword search |

!!! note "Keyword-only search"
    If you don't have an Azure OpenAI embeddings endpoint, you can use the base
    package with keyword search (FTS5) by omitting the `search` section entirely.
    Catalog creation, refresh, search, and reopen do not import or load
    sqlite-vec, create a vector table, or initialize Azure credentials and
    clients.

### How indexing works

At server startup, the catalog indexer:

1. Reads `catalog.yaml` and either scans each source or loads its authoritative manifest
2. Resolves stable identity from the source/path model or an explicit manifest `artifact_id`
3. Inserts metadata into the SQLite `artifacts` table (with FTS5 triggers for keyword indexing)
4. When `embedding_model` is selected, computes embeddings in batches and
   stores them in an on-demand sqlite-vec virtual table

The SQLite database is a rebuildable per-reader index/cache. A manifest-backed
reader opens its own database, calls `load()` before serving, validates the
schema and manifest generation/ETag, and atomically refreshes artifacts, aliases,
history, FTS, vectors, and readiness state. Unchanged generations are skipped;
changed generations create normal artifact revisions. Failed refreshes preserve
the previous successful generation until its stale bound expires. Expiry is
always measured from that source's `last_success_at`, including validation,
embedding, and database-write failures whose transactions roll back.
Legacy timezone-naive success timestamps are interpreted as UTC; malformed
timestamps fail readiness closed.

An explicitly declared manifest `artifact_id` may move to a new logical path in
one generation. The current row and locator move without changing canonical
identity, while retained revisions preserve exact old locators and any
tombstones. Removing the entry in one generation and re-registering the same ID
at a new path later is also supported. Destination paths or aliases retained by
a different artifact fail only that source; independently valid source
generations still commit.

Do not place one writable SQLite file on a shared volume and open it from
multiple pods. The supported deployment model is one writable SQLite file per
reader/pod (or `:memory:`), rebuilt from the manifest after restart. The manifest
is the authority; SQLite is disposable. Historical refresh rows for sources no
longer configured by that reader do not affect readiness or visibility.

### MCP tools exposed

The synchronous SQLite catalog is not yet caller-aware. Its legacy registration
surface is unscoped and should be used only when every catalog entry is already
authorized to every caller with tool access, such as public development data.
These tool names are **not** prefixed with the server name:

| Tool | Description |
|------|-------------|
| `search_data` | Hybrid keyword + vector search over the catalog. Supports filters by domain and source type. |
| `get_artifact` | Get full metadata for a specific artifact by ID |
| `list_domains` | List all unique domain labels in the catalog |
| `query_catalog` | Legacy unscoped read-only SQL over all catalog metadata |

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

### Privileged SQL administration

For v0.2.x compatibility, `query_catalog` remains part of
`register_catalog_tools`. That does not make it authorized: read-only SQLite
prevents writes but does not enforce caller, source, row, or artifact policy.
Use the legacy function only when the entire catalog is already visible to every
caller.

New applications may instead register SQL separately with
`register_catalog_admin_tools` on a separately authenticated and authorized
administrative MCP surface:

```python
from agora_workbench.code_execution.catalog_tools import register_catalog_admin_tools

register_catalog_admin_tools(admin_mcp, catalog_context)
```

The administrative helper is an exposure seam, not an authorization mechanism;
the host remains responsible for protecting that surface.

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

-- sqlite-vec virtual table (created lazily only when vectors are indexed/searched)
```
