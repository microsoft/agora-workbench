# Data-lake migration: 0.2.x to 0.3.0

This page is only for an existing 0.2.x deployment. New users should start with
[Add a data catalog to your server](data-lake.md).

Version 0.3.0 separates stable contracts, catalog implementations, execution
adapters, authorization, and managed publication. Existing compatibility
imports remain available for transition, but new code should use the public
task-oriented modules below.

## Imports

| 0.2.x import area | 0.3.0 import |
| --- | --- |
| `agora_workbench.code_execution.data_access.catalog` | `agora_workbench.data_lake.catalog` |
| fetchers, publishers, credentials, data manager | `agora_workbench.data_lake.execution` |
| artifact/search resolvers | `agora_workbench.data_lake.resolvers` |
| artifact models, protocols, policy, errors | `agora_workbench.data_lake` |
| server catalog composition | `agora_workbench.code_execution.CatalogIntegration` |

Importing `agora_workbench.data_lake` loads contracts only. Import concrete
backends from their documented submodule. Do not depend on implementation
modules or private attributes.

## Configuration

Validate and render an explicit version-1 configuration without overwriting the
source:

```python
from agora_workbench.data_lake.catalog import convert_catalog_config

report = convert_catalog_config("catalog-0.2.yaml")
print(report.rendered_yaml)
```

To write a separate converted file:

```python
convert_catalog_config(
    "catalog-0.2.yaml",
    "catalog-0.3.yaml",
    dry_run=False,
)
```

Then run:

```bash
agora-workbench-data-lake validate --config catalog-0.3.yaml
agora-workbench-data-lake refresh \
  --config catalog-0.3.yaml --database catalog-0.3.db
```

Set `version: 1`. Set a stable `source_id`, especially for manifest sources and
local sources that may move. Make discovery policy explicit:

- `scan` exposes all eligible files below the source root.
- `manifest` exposes only entries in a valid version-1 authoritative manifest
  and requires `source_id` plus `manifest`.

Search defaults to `embedding_model: none`. Configure Azure OpenAI only when
vector search is intentionally enabled; disabled vector and Azure features do
not require unrelated endpoint or credential settings.

## References and identity

Use `ArtifactReference(artifact_id, source_id=..., revision=...)`. The
`source_id` scopes aliases and identity. Omit `revision` to follow the current
revision; specify it to require an exact retained revision. Providers must
honor a pinned revision or reject it, never silently return current data.

The old URI-derived ID helper remains only for legacy import/mapping. New IDs
derive from stable source identity plus normalized logical path, or from an
explicit manifest `artifact_id`. Existing schema-v0 SQLite databases migrate
transactionally when opened. Legacy IDs are retained as source-scoped aliases
where possible.

Opaque load tags returned by catalog integration should be passed through to
execution rather than parsed or reconstructed. Catalog search does not expose
credential-bearing locators to callers.

## Authorization and hosting

Catalog provider capabilities describe backend support, not caller permission.
Compose a `CatalogAuthorizer` for application policy. Storage reader or
contributor/custom roles are a second, independent enforcement layer.

`CatalogIntegration` is the supported `CodeExecutionServer` composition point.
Do not register the legacy and policy-aware catalog tool sets on the same MCP
server. SQLite and manager caches are host-local; use one SQLite writer and a
per-pod rebuildable database/cache unless the application provides external
coordination.

## Rollback and export limits

Before upgrading a production database, retain a filesystem/storage backup and
the old application configuration. For catalog metadata inspection or a
0.2-style recovery import:

```python
from agora_workbench.data_lake.catalog import CatalogDB

db = CatalogDB("catalog.db")
db.open()
try:
    db.export_v0_json("catalog-v0.json")
finally:
    db.close()
```

The v0 export includes current, non-deleted artifacts only. It cannot represent
revision history, tombstones, manifest generations, commit fences, managed
operation receipts, ownership, or all legacy URI spellings. It is not a
lossless downgrade.

After managed writes, rolling application code back does **not** roll storage
or the authoritative manifest back. Immutable revision objects and operation
records may already exist, and older software may not understand or safely
reconcile them. Stop writers first, retain the complete `.agora/` namespace,
export current readable metadata if needed, and restore a known-good storage
snapshot plus matching manifest only under an operator-reviewed recovery plan.
Do not manually decrement a manifest generation or delete operation/revision
objects to simulate rollback.

If a write was interrupted, run the 0.3 reconciliation surface before changing
versions. Reconciliation is ownership- and lease-aware; ad hoc cleanup is not.
