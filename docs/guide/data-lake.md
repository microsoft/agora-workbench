# Add a data catalog to your server

Use the data-lake API when an agent needs to **discover data by metadata** and
then load the selected artifact into a code-execution session.

If you only need to copy fixed files into the server at startup, use
[asset provisioning](working-with-data.md#asset-provisioning). If you only need
to return generated files to a user or storage location, use
[publishers](working-with-data.md#publishing-artifacts). Neither requires a
catalog.

## Choose the workflow

| Goal | Start with |
| --- | --- |
| Let agents search existing local files | A read-only catalog with `discovery: scan` |
| Expose only an approved list of files | A read-only catalog with `discovery: manifest` |
| Register, upload, remove, or promote durable artifacts | An authoritative manifest plus an authorized managed writer |
| Use Azure Blob Storage | The same catalog model with the `azure` extra |
| Upgrade an existing 0.2.x deployment | [0.3 migration guide](data-lake-migration.md) |

For a first deployment, start with keyword-only local search. It requires no
cloud account, embedding service, vector extension, or credentials.

## The five parts

The components have separate jobs:

1. A **source** is the local directory or Blob prefix containing data.
2. A **catalog configuration** says how files are discovered and describes
   their default metadata.
3. The **SQLite index** makes catalog metadata searchable. It is rebuildable;
   it is not the source of truth for managed artifacts.
4. `CatalogIntegration` adds policy-aware discovery and opaque load references
   to a `CodeExecutionServer`.
5. A session's **data manager** resolves a selected reference and caches its
   bytes locally for tool or code execution.

Publishing an output and registering it in a catalog are different operations.
A publisher moves bytes. A managed writer performs an authorized, versioned
catalog mutation.

## Try a local catalog

Install the base package:

```bash
uv add agora-workbench
```

Create a directory with synthetic data:

```bash
mkdir -p data
printf 'day,temperature_c\n2026-01-01,7\n' > data/weather.csv
```

Create, validate, and index a scan catalog:

```bash
uv run agora-workbench-data-lake init \
  --config catalog.yaml \
  --source ./data \
  --source-id local-data \
  --discovery scan

uv run agora-workbench-data-lake validate --config catalog.yaml
uv run agora-workbench-data-lake refresh \
  --config catalog.yaml \
  --database catalog.db
uv run agora-workbench-data-lake search weather \
  --config catalog.yaml \
  --database catalog.db
```

The search result should include `weather.csv`. At this point you have tested
the source policy, configuration, index, and search behavior without starting
an MCP server.

!!! warning "Scan mode exposes the directory"
    Scan discovery catalogs every eligible non-hidden file below the source
    root. Use it only when the whole directory is approved for discovery. For
    selective disclosure, use an
    [authoritative manifest](data-lake-operations.md#zero-cloud-local-quickstart).

## Add the catalog to a server

Add `CatalogIntegration` to an existing `CodeExecutionServer`:

```python
from agora_workbench.code_execution import CatalogIntegration, CodeExecutionServer
from agora_workbench.data_lake import (
    CatalogPolicyMode,
    DevelopmentAllowAllCatalogAuthorizer,
)
from agora_workbench.data_lake.catalog import CatalogConfig

catalog = CatalogIntegration.from_config(
    CatalogConfig.from_yaml("catalog.yaml"),
    authorizer=DevelopmentAllowAllCatalogAuthorizer(),  # local development only
    policy_mode=CatalogPolicyMode.HOMOGENEOUS_SOURCE,
    db_path="catalog.db",
)

server = CodeExecutionServer(
    server_config=config,
    auth_config=auth,
    catalog=catalog,
)
```

The `db_path` points the server at the SQLite index refreshed in the preceding
CLI steps. Here, `config` and `auth` are the server configuration and
authentication objects from your existing server. If you do not have one yet, build
[your first server](../tutorials/first_server/README.md) before adding the
catalog.

The integration adds these MCP tools:

- `search_data` finds authorized artifacts.
- `get_artifact` returns metadata for one artifact.
- `list_domains` lists visible domain labels.
- `get_catalog_capabilities` reports the operations available to the session.

Search results may include an opaque `load_path`. The agent passes that value
unchanged into an `execute_*_code` call; the session resolves, authorizes, and
caches the bytes before execution. Applications should not parse or construct
load paths themselves.

`DevelopmentAllowAllCatalogAuthorizer` is only for local development. In
production, provide a caller-aware `CatalogAuthorizer` or
`authorizer_factory`, and independently grant the server identity only the
storage permissions it needs.

## Move to production deliberately

Before deployment:

- Choose `manifest` discovery when the entire source directory is not approved
  for disclosure.
- Give every source a stable `source_id`.
- Keep keyword-only search unless vector search is a real requirement.
- Use one rebuildable SQLite index per reader or pod; do not share one writable
  SQLite file across pods.
- Treat storage RBAC and application authorization as separate controls.
- Use `AuthorizedManagedCatalogWriter` for durable mutations.
- Set a refresh process and `max_stale_seconds` that match your freshness
  requirement.
- Monitor source refresh state and catalog readiness.

## Where to go next

- [CLI and quickstarts](data-lake-operations.md) — local, managed-write, and
  Azure operating procedures.
- [Working with data](working-with-data.md) — compare provisioning, caching,
  publishing, and catalogs.
- [API reference and extension points](data-lake-api.md) — implement providers,
  resolvers, transfer policy, and managed writes.
- [Support matrix](data-lake-support.md) — supported combinations, limits, and
  release gates.
- [0.3 migration](data-lake-migration.md) — upgrade an existing 0.2.x
  deployment.
