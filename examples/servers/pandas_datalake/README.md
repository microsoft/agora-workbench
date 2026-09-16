# Pandas server with a SQLite object data lake

This example is the smallest complete custom data-lake integration for an
Agora Workbench domain server:

- `execute_pandas_code` provides a pandas and pyarrow environment.
- `search_data`, `get_artifact`, `list_domains`, and
  `get_catalog_capabilities` come from `CatalogIntegration`.
- Catalog metadata, immutable revisions, and object bytes all live in one local
  SQLite database.
- A custom `SQLiteObjectFetcher` streams selected BLOBs into each execution
  session's cache, so a catalog result's opaque `load_path` works with
  `pd.read_csv`.
- `CatalogIntegration.fetcher_factory` creates one `SQLiteObjectFetcher` per
  session; the standard data manager handles resolver binding, cache
  invalidation, and cleanup.

The server therefore needs no custom `SessionManager`:

```python
catalog = CatalogIntegration(
    ResourceLease(SQLiteCatalogProvider(lake), ResourceOwnership.OWNED),
    authorizer=DevelopmentAllowAllCatalogAuthorizer(),
    fetcher_factory=lambda context: SQLiteObjectFetcher(lake),
)
server = PandasDataLakeServer(config, auth_config=auth, catalog=catalog)
```

The example intentionally uses `DevelopmentAllowAllCatalogAuthorizer` and
no-op MCP authentication. It binds to loopback by default and is for local
development only.

## Run it

From the repository root:

```bash
PANDAS_DATALAKE_DB=/tmp/pandas-lake.sqlite3 \
  uv run python -m examples.servers.pandas_datalake.server.pandas_datalake_server
```

On first start, the server creates the SQLite schema and seeds `sales.csv` and
`customers.csv`. The server listens on `http://127.0.0.1:8022`.

An agent can then:

1. Call `search_data(query="sales", domain="tabular", source_type="sqlite")`.
2. Copy the returned `load_path` unchanged into
   `execute_pandas_code`.
3. Load it with pandas:

   ```python
   path = "<blob>catalog-v1:...</blob>"
   sales = pd.read_csv(path)
   print(sales.groupby("region")["revenue"].sum())
   ```

The execution middleware replaces the complete string literal with a local
`Path`; the kernel never opens the SQLite database directly.

## Add or update an object

The storage API keeps every revision and advances the current pointer:

```python
from examples.servers.pandas_datalake.server.sqlite_data_lake import SQLiteObjectLake

lake = SQLiteObjectLake("/tmp/pandas-lake.sqlite3")
lake.initialize()
reference = lake.put_object(
    "inventory.csv",
    b"sku,quantity\nA-1,12\nB-2,7\n",
    name="inventory.csv",
    description="Current inventory snapshot.",
    media_type="text/csv",
    metadata={"domain": "tabular", "source_type": "sqlite"},
)
print(reference)
```

Uploading the same `artifact_id` again creates revision 2 rather than replacing
revision 1. Catalog-generated load paths are pinned to the revision shown in
the search result.
