# Data-lake CLI and quickstarts

**New to the data-lake API?** Read
[Add a data catalog to your server](data-lake.md) first for the component model
and the shortest path from local data to agent-visible search.

The `agora-workbench-data-lake` command is the supported administrative
surface for local catalog initialization, validation, refresh, search, external
registration, and managed-write reconciliation. It calls the same public
library APIs documented in [Data-lake API](data-lake-api.md); it does not
provision storage, identities, search services, or application hosts.

Use this page when you are operating a catalog:

- **Local read-only:** follow the zero-cloud quickstart.
- **Selective disclosure:** choose manifest discovery instead of scanning.
- **Durable writes:** use the authorized managed-write example.
- **Azure:** bring an existing storage account, container, identity, and
  application host.

## Install

```bash
uv add agora-workbench
# Add only the capabilities the deployment uses:
uv add "agora-workbench[azure]"
uv add "agora-workbench[catalog-vector]"
```

The base package supports local sources and SQLite FTS5 keyword search. Azure
Blob access requires the `azure` extra. Vector search requires
`catalog-vector`; Azure Blob plus Azure OpenAI vector search uses both extras.
Disabled optional features do not require their endpoints, deployments, or
credentials. Selecting a missing feature fails with an installation command
rather than silently changing modes.

## Zero-cloud local quickstart

Run the synthetic, read-only example from a clean checkout:

```bash
uv run python examples/data_lake/local_read_only.py \
  --workspace .workbench-quickstart/scan \
  --discovery scan
```

This creates synthetic CSV data, explicit version-1 configuration, and a local
SQLite index. **Scan policy:** `discovery: scan` discloses every non-hidden,
non-reserved regular file below the validated source root. Use it only where
that entire tree is approved for catalog discovery.

The authoritative alternative discloses only approved manifest entries:

```bash
uv run python examples/data_lake/local_read_only.py \
  --workspace .workbench-quickstart/manifest \
  --discovery manifest
```

The manifest example is versioned (`version: 1`, `generation: 1`). A missing,
malformed, unsupported, or otherwise invalid manifest is an error. Workbench
never falls back from configured manifest discovery to directory scanning.

The equivalent administrative flow is:

```bash
agora-workbench-data-lake init \
  --config catalog.yaml \
  --source ./data \
  --source-id local-data \
  --discovery scan
agora-workbench-data-lake validate --config catalog.yaml
agora-workbench-data-lake refresh --config catalog.yaml --database catalog.db
agora-workbench-data-lake search weather \
  --config catalog.yaml --database catalog.db
```

For a new local approved manifest:

```bash
agora-workbench-data-lake init \
  --config approved-catalog.yaml \
  --source ./approved-data \
  --source-id approved-data \
  --discovery manifest \
  --manifest .agora/manifest.json \
  --create-empty-manifest
```

`validate` enumerates sources and validates manifest contents without writing a
catalog database or computing embeddings. `refresh` updates SQLite
transactionally per successful source. Both commands return nonzero on invalid
configuration or manifests and print a concise `ERROR:` message to stderr.

## Managed registration and promotion

Publication and catalog mutation are separate from read-only refresh. The
managed writer snapshots immutable revision bytes first, then conditionally
commits a new manifest generation. Use application authorization in addition
to filesystem or cloud permissions.

The runnable promotion example composes an explicit authorizer around the
public writer:

```bash
uv run python examples/data_lake/managed_promotion.py \
  --workspace .workbench-quickstart/managed
```

For an existing object inside a local managed root, the CLI supports external
registration. It requires a checksum and an application authorization factory:

```bash
agora-workbench-data-lake register \
  --root ./managed-lake \
  --source-id approved-data \
  --operation-id register-2026-09-15 \
  --path approved/input.csv \
  --storage-path incoming/input.csv \
  --checksum-sha256 SHA256_HEX \
  --authorization-factory my_application.catalog_policy:create_authorizer \
  --caller-id operator@example.invalid
```

`module.path:factory` must return the public `CatalogAuthorizer` protocol. The
explicit `--allow-development-writes` alternative uses the development-only
allow-all authorizer and must not be used as production authorization.

Recover abandoned local operations after the configured lease/grace period:

```bash
agora-workbench-data-lake reconcile \
  --root ./managed-lake \
  --source-id approved-data \
  --grace-seconds 300
```

Reconciliation reports recovered, removed, deferred, and failed operation IDs.
Any failure produces a nonzero exit. Run it under an administrative application
identity; storage permission alone is not end-user authorization.

The CLI intentionally limits managed administration to the local public backend
today. Applications using Azure managed writes compose caller-owned
`ContainerClient`, `BlobManagedStorage`, `ManagedCatalogWriter`, and
`AuthorizedManagedCatalogWriter` objects in their host. This avoids inventing
an incomplete CLI credential or authorization model.

## Bring your own Azure Storage

The actual resource floor is:

1. one Azure Storage account and Blob container; and
2. an application host for the Workbench server or administrative process.

Azure AI Search and an embedding endpoint are optional. Keyword-only catalog
search needs neither.

The checked-in `examples/data_lake/catalog.azure.yaml` is a generic,
credential-free configuration:

```yaml
version: 1
sources:
  - source_id: approved-observations
    path: az://exampleaccount/example-container/approved/
    discovery: manifest
    manifest: .agora/manifest.json
    max_stale_seconds: 300
search:
  embedding_model: none
```

Install the Azure extra and supply credentials through the application host's
normal Azure identity chain:

```bash
uv add "agora-workbench[azure]"
cp examples/data_lake/catalog.azure.yaml catalog.azure.yaml
agora-workbench-data-lake validate --config catalog.azure.yaml
agora-workbench-data-lake refresh \
  --config catalog.azure.yaml --database /var/lib/agora/catalog.db
```

`examples/data_lake/azure.manifest.example.json` shows the corresponding
versioned manifest shape. Upload approved data and the manifest through your
normal deployment/data-governance process; Workbench does not create the
account, container, objects, roles, or host.

Do not put account keys or SAS query strings in configuration. Prefer the
host's managed identity, workload identity, developer login, or another
caller-owned `TokenCredential`. Importing the library and starting a local
keyword-only catalog do not contact Azure or provision resources.

For readers, grant the host identity the least storage data-plane permission
needed to list and read the configured container/prefix (commonly **Storage
Blob Data Reader**). Managed publication needs contributor-level data-plane
rights or a custom role containing the exact object read/create/write/delete
actions required by the selected lifecycle. Azure RBAC controls storage access;
the application's `CatalogAuthorizer` separately decides which caller may
search, resolve, register, upload, promote, or remove.

Keep the authoritative manifest version at `1` and increase `generation`
monotonically for every committed change. A lower generation than the cached
generation is rejected. Failed refresh preserves the last valid generation
only within `max_stale_seconds`; readiness and per-source state disclose when
results are stale.

## Operating limits and observability

- Provider `capabilities()` values state which read or write operations a
  source implementation supports. They do not grant caller permission. The CLI
  exposes only completed combinations: config/source validation, SQLite
  refresh and search, and local managed registration/reconciliation. Unsupported
  provider operations fail explicitly rather than being inferred from a method
  name or silently emulated.
- Manifests are limited to 4 MiB and 10,000 artifact entries.
- Public SQLite provider pages are limited to 100 results, cursors stop beyond
  a 10,000-result offset, and generic request limits cannot exceed 1,000.
- SQLite has one writer. Use one refresh/administrative writer per database.
  In multi-pod deployments, give each pod a rebuildable local catalog/cache or
  coordinate refresh externally; do not place one SQLite file on a shared
  multi-writer volume.
- `DataLakeDataManager` caches are per server/session or pod according to host
  composition. They are not a distributed consistency layer.
- `refresh` emits changed counts and source refresh state, including attempted
  and successful generations, timestamps, artifact counts, and errors.
- Manifest provider `readiness()` reports ready/stale status and the retained
  generation. Transfer APIs expose structured diagnostics, request context,
  byte counts, checksums, duration, and credential-sanitized resource names.
- Configuration, manifest, permission, backend, conflict, precondition,
  timeout, quota, checksum, and reconciliation failures use the public typed
  error model. CLI failures return nonzero and never print a traceback for
  expected operator errors.

Treat catalog locators as internal fetch values: they may be
credential-capable. Logs and user-facing output must use sanitized references.
