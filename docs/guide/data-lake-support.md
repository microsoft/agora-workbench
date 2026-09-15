# Data-lake v0.3.0 support and release acceptance

This page is for deployment owners and release maintainers. New users should
start with [Add a data catalog to your server](data-lake.md).

It records the supported v0.3.0 combinations and the gates used before release.
It is intentionally narrower than the set of components an application could
assemble from private implementation details.

## Support matrix

| Area | Supported in v0.3.0 | Authentication / dependency | Acceptance gate |
| --- | --- | --- | --- |
| Local scan catalog | Read-only discovery, SQLite FTS5 search, filters, resolution and bounded local transfer | Base package; application `CatalogAuthorizer` | Offline default CI |
| Local authoritative manifest | Read-only discovery, generations, aliases, revisions, staleness/readiness and resolution | Base package; application `CatalogAuthorizer` | Offline default CI |
| Local managed catalog | Register, upload, promote, remove, manifest CAS, receipts and reconciliation | Base package plus application authorization and filesystem permission | Offline default CI |
| Azure Blob scan/manifest catalog | Read-only listing, manifest refresh and bounded Blob transfer | `azure` extra; caller-owned or host identity with Blob data-plane read permission | Opt-in live Azure |
| Azure Blob managed catalog | Register, upload, promote, remove, ETag CAS and reconciliation over a caller-owned `ContainerClient` | `azure` extra; Blob contributor/custom data-plane role plus application authorization | Azurite for storage mechanics; live Azure for Entra ID/RBAC |
| Keyword search | SQLite FTS5, top-k, `domain` and `source_type` filters | Base package only; no vector or cloud SDK | Offline default CI with optional imports blocked |
| Vector search | SQLite vector/hybrid search and Azure OpenAI query embeddings | `catalog-vector`; add `azure` for Azure identity/storage | Unit/integration gates; deployment-specific live validation |
| MCP discovery | Policy-aware `search_data`, `get_artifact`, `list_domains`, and `get_catalog_capabilities` | `CatalogIntegration` and application authorization | Offline default CI |
| Administrative SQL | `query_catalog` only on a separately authorized administrative MCP surface; SQLite enforces query-only execution | Base package; independent administrative access control | Offline default CI |
| No catalog configured | Existing execution/session behavior and tool surface remain unchanged | Base package | Offline default CI |

Provider capabilities describe implementation support, not caller permission.
Storage RBAC and `CatalogAuthorizer` are independent checks.

### Search and isolation boundary

The public provider supports top-k/pagination, exact `domain` and
`source_type` filters, source-scoped aliases, and the bounded `list_domains`
MCP helper. v0.3.0 does **not** expose a general facet/aggregation API.
Applications needing arbitrary metadata SQL must mount `query_catalog` on a
separate administrative server; policy-aware user discovery intentionally does
not expose it. Per-artifact search isolation requires a backend-specific
`CatalogPolicyEnforcer`; the built-in SQLite provider's supported production
profile is homogeneous authorization per source.

## Supported budgets and limits

Hard validation limits and measured acceptance budgets are different:

- Manifest validation accepts at most 10,000 entries and 4 MiB. This is a hard
  format limit, not a latency promise at that size.
- Default CI measures a 1,000-artifact synthetic local manifest on Linux:
  initial refresh under 15 seconds, explicit next-generation convergence under
  10 seconds, median keyword-search latency under 250 ms over 20 searches, and
  Python peak allocation below 128 MiB.
- A configured manifest generation becomes visible after a successful explicit
  refresh. There is no built-in distributed/background refresh interval.
- Failed refresh may retain the last valid generation only for the configured
  `max_stale_seconds`; the default is 300 seconds.
- SQLite supports one writer. Use a rebuildable database per pod/reader and
  coordinate refresh externally. Managed manifests use optimistic CAS and can
  have multiple writers.

These budgets are regression floors for the documented CI environment, not
claims for production datasets, network storage, arbitrary hardware, or 10,000
entry manifests.

## Test gates

### Offline release acceptance

The normal suite requires no Docker, network, or credentials:

```bash
uv run pytest -m "not live"
```

It covers local filesystem roundtrips through managed writes, manifest refresh,
policy-aware MCP discovery, alias resolution, session resolution/transfer,
two-principal metadata/content/write isolation, concurrent manifest writers,
interrupted-operation reconciliation, two-reader generation convergence,
0.2.x fixture migration/export, optional dependency boundaries, startup
failure, shutdown cleanup, and the measured budgets above.

### Actual Azurite

Azurite is opt-in and separately marked. Start a pinned Blob emulator:

```bash
docker run --rm --name agora-azurite \
  -p 10000:10000 \
  mcr.microsoft.com/azure-storage/azurite:3.35.0 \
  azurite-blob --blobHost 0.0.0.0 --skipApiVersionCheck
```

In another shell:

```bash
export AGORA_AZURITE_CONNECTION_STRING='UseDevelopmentStorage=true'
uv run pytest src/agora_workbench/data_lake/tests/test_azurite_acceptance.py \
  -m azurite -v
```

The tests create uniquely named containers and prove two public paths against
actual emulator network I/O:

- `BlobManagedStorage` plus `ManagedCatalogWriter` uploads, downloads,
  registration transfer, ETag manifest CAS, interruption and reconciliation;
- `SQLiteCatalogProvider` plus `CatalogIntegration`, policy-aware MCP
  `search_data`, opaque execution-reference resolution, `DataLakeDataManager`
  and `BlobFetcher` transfer of actual emulator bytes.

The execution composition supplies `BlobFetcher.account_endpoints` for the
`devstoreaccount1` account. Explicit endpoints may use HTTPS, or plain HTTP
only on loopback for emulators; credentials, query strings, fragments and dot
segments are rejected. The canonical artifact locator remains
`az://account/container/path`, and configured account/container/prefix scopes
still apply.

`CatalogConfig` Blob enumeration intentionally continues to target canonical
Azure production endpoints; the Azurite acceptance provider is populated
through the public SQLite catalog API rather than weakening source URI
validation. Azurite does not provide Entra ID or Azure RBAC, so production
catalog enumeration, identity and RBAC remain live-Azure gates. Every Azurite
test deletes its container. If the variable is absent, the gate skips cleanly.

### Live Azure

Use dedicated, disposable prefixes in one existing test storage account. Do
not use production data. Configure:

```bash
export AGORA_LIVE_AZURE_ACCOUNT_URL='https://ACCOUNT.blob.core.windows.net'
export AGORA_LIVE_AZURE_ALLOWED_CONTAINER='agora-acceptance-allowed'
export AGORA_LIVE_AZURE_DENIED_CONTAINER='agora-acceptance-denied'
export AGORA_LIVE_AZURE_IDENTITY_MODE='managed'  # managed, workload, or delegated
# Optional for user-assigned managed identity:
export AGORA_LIVE_AZURE_MANAGED_CLIENT_ID='CLIENT-ID'
# Optional tenant selection for delegated Azure CLI identity:
export AGORA_LIVE_AZURE_TENANT_ID='TENANT-ID'

uv run pytest src/agora_workbench/data_lake/tests/test_azure_live_acceptance.py \
  -m live -v
```

Run the gate once for each identity mode the release/deployment claims:

- `managed`: `ManagedIdentityCredential`;
- `workload`: federated `WorkloadIdentityCredential`;
- `delegated`: the explicitly logged-in `AzureCliCredential`.

The selected identity needs Blob data contributor (or an equivalent custom
data-plane role) on the allowed container and must have no Blob data-plane
access to the denied container. Both containers must already exist. The test
requires an actual 403 from the denied container, performs concurrent ETag-CAS
writes and manifest catalog reads in the allowed container, and removes every
blob under its unique prefix. It never creates/deletes accounts, containers,
roles, or credentials. Missing configuration skips all live operations.

## v0.3.0 release acceptance checklist

- [x] Local public catalog, MCP, resolution, transfer and managed-write
      roundtrip is covered in offline CI.
- [x] Read-only and managed profiles, two principals, source-scoped aliases,
      top-k/filter behavior and absence of user-facing administrative SQL are
      asserted together.
- [x] Multi-writer CAS, interrupted commits/orphans, reconciliation and
      multi-reader generation convergence are covered.
- [x] Synthetic 0.2.x imports, config, SQLite and local/Blob references migrate;
      post-mutation v0 export is verified as the documented lossy rollback aid.
- [x] Base keyword-only operation is tested with Azure, OpenAI and sqlite-vec
      imports blocked.
- [x] Invalid startup/readiness failure and idempotent shutdown cleanup are
      covered.
- [x] Actual Azurite and live-Azure tests are separately gated and clean up
      their resources.
- [x] Deterministic catalog-size, refresh, latency and peak-memory budgets run
      in default CI.
- [ ] Live Azure is not run by regular CI; release operators must run and record
      each claimed identity mode against dedicated resources.
- [ ] General facets, distributed cache coherence, automatic cross-pod refresh,
      SQLite multi-writer databases, arbitrary emulator endpoints, lossless
      downgrade after managed writes, and production-scale/network latency
      guarantees are explicitly deferred.
