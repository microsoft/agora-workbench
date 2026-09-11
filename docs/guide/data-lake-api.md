# Data-lake API

Use the data-lake API to describe artifacts, search a catalog, resolve artifact
IDs to storage locations, and connect those artifacts to code execution.

Choose the import path that matches your task:

| Task | Import from |
| --- | --- |
| Define artifact records or implement a catalog/resolver | `agora_workbench.data_lake` |
| Use the built-in SQLite catalog and indexer | `agora_workbench.data_lake.catalog` |
| Use manifest-backed provider/resolver adapters | `agora_workbench.data_lake.catalog` / `agora_workbench.data_lake.resolvers` |
| Use the Azure AI Search resolver | `agora_workbench.data_lake.resolvers` |
| Configure fetchers, publishers, credentials, or `DataLakeDataManager` | `agora_workbench.data_lake.execution` |

Importing `agora_workbench.data_lake` does not start a server, create an
execution session, or load cloud SDK modules.

## Represent artifacts

The API separates an artifact's identity, storage location, and display
metadata:

- `ArtifactReference` identifies an artifact by `source_id` and `artifact_id`.
  Its optional `revision` pins a retained revision; omitting it follows the
  current revision. Providers and adapters must either honor a pinned revision
  exactly or reject it explicitly; they must not silently return current data.
- `StorageLocator` contains the physical URI used to retrieve it.
- `ArtifactPresentation` contains human-facing metadata.
- `DownloadInfo` optionally provides a user-facing download link and expiry.

For example:

```python
from agora_workbench.data_lake import (
    ArtifactPresentation,
    ArtifactReference,
    CatalogArtifact,
    StorageLocator,
)

artifact = CatalogArtifact(
    reference=ArtifactReference("hourly-wind", source_id="weather"),
    presentation=ArtifactPresentation(
        "hourly-wind.parquet",
        media_type="application/x-parquet",
    ),
    locator=StorageLocator("file:///data/hourly-wind.parquet"),
)
```

## Implement a catalog provider

Implement the async `CatalogProvider` protocol when artifacts come from your
own manifest, database, service, or other catalog. A provider supports
`search`, `list`, `get`, and `resolve` operations:

```python
from agora_workbench.data_lake import (
    READ_OPERATIONS,
    ArtifactNotFoundError,
    ArtifactPresentation,
    ArtifactReference,
    CatalogArtifact,
    CatalogProvider,
    ListRequest,
    Page,
    RequestContext,
    ResolvedArtifact,
    SearchRequest,
    SourceCapabilities,
    StorageLocator,
)


class MemoryCatalog:
    def __init__(self, artifacts: tuple[CatalogArtifact, ...]):
        self.artifacts = artifacts

    async def capabilities(self) -> tuple[SourceCapabilities, ...]:
        return (SourceCapabilities("weather", READ_OPERATIONS),)

    async def search(
        self, request: SearchRequest, context: RequestContext
    ) -> Page[CatalogArtifact]:
        matches = tuple(
            artifact
            for artifact in self.artifacts
            if request.query.lower() in artifact.presentation.name.lower()
        )
        return Page(matches[: request.page.limit])

    async def list(
        self, request: ListRequest, context: RequestContext
    ) -> Page[CatalogArtifact]:
        return Page(self.artifacts[: request.page.limit])

    async def get(
        self, reference: ArtifactReference, context: RequestContext
    ) -> CatalogArtifact:
        try:
            return next(a for a in self.artifacts if a.reference == reference)
        except StopIteration as exc:
            raise ArtifactNotFoundError(
                f"Unknown artifact: {reference.artifact_id}",
                resource_id=reference.artifact_id,
                operation="get",
            ) from exc

    async def resolve(
        self, reference: ArtifactReference, context: RequestContext
    ) -> ResolvedArtifact:
        artifact = await self.get(reference, context)
        if artifact.locator is None:
            raise ArtifactNotFoundError(
                f"Artifact has no storage locator: {reference.artifact_id}",
                resource_id=reference.artifact_id,
                operation="resolve",
            )
        return ResolvedArtifact(reference, artifact.locator)


artifact = CatalogArtifact(
    reference=ArtifactReference("hourly-wind", source_id="weather"),
    presentation=ArtifactPresentation("hourly-wind.parquet"),
    locator=StorageLocator("file:///data/hourly-wind.parquet"),
)
catalog: CatalogProvider = MemoryCatalog((artifact,))
```

## Identity, revisions, and compatibility

Catalog identity is independent of physical storage. Configure a stable
`source_id` and identify artifacts by their normalized source-relative path:

```yaml
sources:
  - source_id: weather-observations
    path: /mounted/data/weather
```

The SQLite index persists the resulting logical artifact ID. Moving the local
root while retaining `source_id` and relative paths preserves artifact identity;
local roots themselves are deliberately not portable IDs. A rename or move to a
different relative path creates a new artifact and tombstones the old one. The
indexer does not guess rename relationships from size, timestamps, or content.
In authoritative manifest mode, an explicitly declared stable `artifact_id` is
the rename signal: moving that ID to a new registered path keeps the canonical
identity, appends retained history for the old and new locations, and updates
current resolution to the new locator.

Each change appends an `artifact_revisions` row. `content_revision` and
`metadata_revision` are separate opaque change tokens. `checksum_sha256`, when
supplied, is optional integrity metadata and is never the artifact identity.
Deleted artifacts remain as tombstones and retain history until an operator
explicitly calls `CatalogDB.purge_deleted(before=...)`.

The indexer supplies provider change tokens: local size plus `mtime_ns`, or a
Blob ETag. Direct `CatalogDB.upsert_artifact()` callers should supply an explicit
`content_revision` or `checksum_sha256` when reliable same-size content-change
detection matters. The fallback size token intentionally does not change merely
because indexing ran again or the storage location moved.

Azure Blob locations in `az://`, Blob HTTPS, and ADLS Gen2 `abfss://` forms are
canonicalized to `az://account/container/object`. SAS/query parameters and
fragments do not participate in identity, account/container case is normalized,
and object-name case is preserved. Blob names returned by the Azure SDK are
quoted exactly once, so a literal `%41` object remains distinct from `A`.

Schema-v0 databases are migrated transactionally to the current version. Their
original URI-derived IDs and IDs derived from canonical Azure URIs remain
resolvable through `artifact-id` aliases. Original and canonical storage
locations are stored as actual values in the `storage-uri` namespace. Existing
sqlite-vec embeddings are preserved and re-keyed during migration when present.
New aliases use the explicit `namespace:value` form; unqualified opaque IDs use
the `artifact-id` namespace. Aliases are source-scoped, so the same imported
alias may exist in multiple sources; callers must provide `source_id` when an
unqualified lookup is ambiguous. Alias and canonical-ID collisions are rejected.
`artifact_id_from_uri()` remains available only as a legacy import/mapping helper
and does not define new canonical identities. Databases with an unknown newer
schema fail before mutation.

`CatalogDB.export_v0_json()` provides deterministic, CLI-independent recovery
for downgrade or inspection. It emits current, non-deleted artifacts using the
v0 record fields and prefers a retained legacy `artifact-id` alias where one
exists:

```python
catalog.export_v0_json("catalog-v0.json")
```

The export intentionally omits revision history and tombstones because schema
v0 cannot represent them. It is a recovery/import artifact, not a guarantee that
every retained legacy ID can be recomputed from the exported URI: canonicalized
Azure locators may differ textually from the URI that originally produced a v0
ID.

Indexer reconciliation is source-transactional in the safety sense: stale
paths are tombstoned only for sources that enumerated successfully. Missing or
unreadable local roots and failed Blob listings leave prior records untouched;
a successfully enumerated empty source tombstones its former contents. Batch
database upserts are atomic.

Providers receive the same `RequestContext` on every operation. Its fields are
copied into immutable mappings so they can be propagated safely. They do not
define authorization or cache identity.

Use `SearchRequest.source_ids` and `ListRequest.source_ids` to restrict an
operation to specific sources. An empty tuple means all sources. Treat
pagination cursors as opaque provider values; callers should not parse them.
Page limits cannot exceed `MAX_PAGE_LIMIT` (1000).

## Caller-aware policy composition

Each `SourceCapabilities` value reports the operations supported by one
`source_id`; check these values rather than inferring support from method
presence. Provider support is not caller authorization. Compose an
application-defined `CatalogAuthorizer` outside the provider with
`AuthorizedCatalogProvider`; its caller-aware `capabilities(context)` result is
the intersection of provider support and policy.

```python
from agora_workbench.data_lake import (
    AuthorizedCatalogProvider,
    CatalogAuthorizationRequest,
    CatalogPolicyMode,
    RequestContext,
)


class TenantPolicy:
    async def authorize(
        self,
        request: CatalogAuthorizationRequest,
        context: RequestContext,
    ) -> bool:
        tenant = context.attributes.get("tenant")
        allowed_sources = {
            "public": {"weather"},
            "research": {"weather", "experiments"},
        }.get(tenant, set())
        return request.source_id in allowed_sources


authorized_catalog = AuthorizedCatalogProvider(
    catalog,
    TenantPolicy(),
    mode=CatalogPolicyMode.HOMOGENEOUS_SOURCE,
)
```

Policy mode is always explicit:

- `HOMOGENEOUS_SOURCE` grants or denies an operation for an entire source. The
  wrapper constrains `search` and `list` before provider ranking/pagination and
  validates that provider results remain inside the authorized sources.
- `PER_ARTIFACT` requires a backend-specific `CatalogPolicyEnforcer`. That
  adapter must apply artifact policy before ranking, pagination, aggregations,
  alias resolution, and lookup errors. The wrapper refuses this mode without an
  enforcer; it never silently falls back to filtering one top-k page.

`DenyAllCatalogAuthorizer` is an explicit fail-closed baseline.
`DevelopmentAllowAllCatalogAuthorizer` is an explicit development-only choice;
`AuthorizedCatalogProvider` has no implicit allow policy.

Denied `get` and `resolve` calls are reported as a generic not-found result, so
backend error differences and resource identifiers do not disclose artifact
existence. Returned references must exactly match the requested logical identity
`(source_id, artifact_id)`; a provider or enforcer that substitutes another
artifact fails closed. Alias resolution must therefore preserve the requested
logical reference at this boundary or be modeled as a separate, policy-aware
lookup operation. Search and list requests silently omit unauthorized sources
before the provider is called. A provider remains responsible for honoring
ordinary source and cursor constraints, but it does not receive authority to
decide which callers are allowed.

### Request state, caches, and revocation

Treat `RequestContext` attributes as request-scoped and potentially sensitive.
Do not retain credentials, bearer tokens, session objects, or the context itself
in process-wide provider caches or logs. Cache only non-sensitive provider data,
or partition caller-visible caches by an application-defined authorization scope
that cannot collide across principals.

The policy wrapper re-evaluates authorization on every operation and does not
cache decisions. Applications that cache policy decisions must define bounded
TTL and explicit invalidation semantics; otherwise revocation cannot take effect
promptly. Pagination cursors and backend query caches must remain bound to the
current authorization constraints and must fail closed if those constraints
cannot be reapplied. `AuthorizedCatalogProvider` rejects a non-null cursor when
authorization narrows the request's source set. When the set is unchanged, the
provider remains responsible for validating that its opaque cursor was minted
for the same source and query constraints.

### Raw SQL

Raw catalog SQL is not part of `CatalogProvider` or `PolicyEnforcedCatalog`.
SQLite read-only mode prevents writes; it is not row-, artifact-, source-, or
caller-level authorization. For v0.2.x compatibility, the legacy
`register_catalog_tools` function still includes `query_catalog`, but that whole
surface is unscoped and is safe only when every artifact and metadata row is
already authorized to every caller with tool access. New applications should
prefer the explicit `register_catalog_admin_tools` extension on a separately
authenticated and authorized administrative surface.

## Errors

Provider error categories include `InvalidRequestError`,
`ArtifactNotFoundError`, `UnsupportedOperationError`, `PermissionDeniedError`, and
`BackendUnavailableError`. Provider implementations should raise these typed
errors at the public boundary and retain backend exceptions as their causes when
useful. An unclassified `DataLakeError` uses the `INTERNAL` code and should not
be treated as a retryable backend outage.

`CatalogProvider` and `ArtifactResolver` are runtime-checkable protocols only for
basic structural checks. `isinstance()` verifies member presence, not signatures,
async behavior, return types, or correct behavior.

## Use the built-in SQLite catalog

Load catalog configuration from YAML, open a SQLite catalog, index its sources,
and run a keyword search:

```python
from agora_workbench.data_lake.catalog import (
    CatalogConfig,
    CatalogDB,
    CatalogIndexer,
)


async def search_catalog():
    config = CatalogConfig.from_yaml("catalog.yaml")
    catalog = CatalogDB("catalog.db")
    catalog.open()
    try:
        indexer = CatalogIndexer(config=config, db=catalog)
        indexed_count = await indexer.index()
        results = catalog.search("hourly wind")
        return indexed_count, results
    finally:
        catalog.close()
```

See [Working with data](working-with-data.md#data-catalog) for the
`catalog.yaml` format, source configuration, and search options.

## Use an authoritative manifest provider

`ManifestCatalogProvider` composes the existing versioned SQLite identity,
revision, alias, search, and refresh implementation with authoritative local or
Azure Blob manifests:

```python
from agora_workbench.data_lake import ListRequest, RequestContext
from agora_workbench.data_lake.catalog import CatalogConfig, ManifestCatalogProvider
from agora_workbench.data_lake.resolvers import CatalogArtifactResolver

config = CatalogConfig.from_yaml("catalog.yaml")
provider = ManifestCatalogProvider(
    config,
    db_path="/var/run/agora/catalog-reader.db",
    max_stale_seconds=300,
)

await provider.load()
readiness = provider.readiness()
page = await provider.list(ListRequest(), RequestContext())
resolver = CatalogArtifactResolver(provider, source_id="approved-model-inputs")
qualified_name = await resolver.resolve(page.items[0].reference.artifact_id)
```

The provider performs no caller authorization. Wrap it with
`AuthorizedCatalogProvider` exactly as any other `CatalogProvider`; credentials
used to read a Blob manifest are storage-host credentials and remain separate
from caller policy.

`load()` is required before serving. On failure it raises
`BackendUnavailableError` and records the failed attempt. If a valid generation
already exists, it is preserved within its stale bound; otherwise the provider
remains unavailable but may be retried after the manifest or credentials are
fixed. Caller-facing errors identify affected source IDs and generic failure
categories without exposing local absolute paths, SAS values, or backend
response details. `readiness()` distinguishes ready, bounded-stale, and
unavailable states. Once the configured stale bound expires, catalog operations
fail rather than serving indefinitely stale approvals. The age check uses
`last_success_at` for each configured source even when validation, embedding, or
SQLite writes fail after enumeration and the persisted transaction remains at
its prior successful status. Refresh rows belonging to removed or unrelated
sources in a reused per-reader cache are ignored.

Pinned `ArtifactReference.revision` values are passed to the shared retained
history store. The exact revision is returned or `ArtifactNotFoundError` is
raised; pinned requests never follow current. This remains true across manifest
moves: an old live revision resolves its old locator, a tombstone revision is
not returned as live data, and an unqualified current request resolves the new
locator. `CatalogArtifactResolver` is the compatibility adapter for the existing
string-based execution resolver and is bound to one source, so aliases remain
source-scoped.

`SQLiteCatalogProvider` adapts an already-open, caller-owned `CatalogDB`. It
supports the public list/search/get/resolve surface, source constraints,
`domain` and `source_type` filters, and request-bound opaque cursors. SQLite
search pages are limited to 100 records, matching the current deterministic
search candidate bound.

Manifest-backed SQLite files are private per reader. Do not share one writable
database between pods; use a pod-local file or `:memory:` and rebuild from the
authoritative manifest on startup. `ManifestCatalogProvider` owns this database,
keeps it open after a recoverable initial load failure so `load()` can be
retried, closes it on initial-load cancellation, supports idempotent `aclose()`,
and may be used as an async context manager. `SQLiteCatalogProvider` instead
borrows its already-open `CatalogDB`; the caller retains cleanup ownership.

## Provide a custom artifact resolver

An artifact resolver maps an opaque artifact ID, such as the value inside
`<blob>hourly-wind</blob>`, to a qualified storage name or URL. Resolver
implementations are structural and do not need to inherit from a base class:

```python
from agora_workbench.data_lake import ArtifactResolver
from agora_workbench.data_lake.execution import DataLakeDataManager


class ManifestResolver:
    def __init__(self, locations: dict[str, str]):
        self.locations = locations

    @property
    def unavailable_reason(self) -> str | None:
        return None

    async def resolve(self, artifact_id: str) -> str:
        return self.locations[artifact_id]


resolver: ArtifactResolver = ManifestResolver(
    {"hourly-wind": "file:///data/hourly-wind.parquet"}
)
manager = DataLakeDataManager(artifact_resolver=resolver)
```

`DataLakeDataManager` calls `resolve` on each manager cache miss. Add caching
inside the resolver if the backend requires it. A resolver may also define
`async def aclose(self) -> None`; the manager calls it during cleanup.

## Ownership and lifetime

Create catalog indexes and their backing clients once per process, share them
across requests, and close them when the application shuts down.
`DataLakeDataManager`, its local cache, and its fetcher clients belong to one
execution session and must be closed when that session ends.

The synchronous `CatalogDB` compatibility API is not a `CatalogProvider` adapter
and cannot correctly apply per-artifact policy after its existing top-k search.
Its existing `register_catalog_tools(mcp, context, activity_publisher=None)`
signature and four tools remain available for v0.2.x compatibility, with a
security warning at registration. This unscoped surface is appropriate only
when the entire catalog is already authorized to all callers, such as public
development data. Production mixed-trust deployments must use a caller-aware
provider adapter and policy composition rather than treating tool access or
SQLite read-only access as authorization.

`ResourceLease` records whether a supplied resource is `OWNED` or `BORROWED`.
Close only owned resources. A resolver or manager may close a client it creates,
but must not close a credential or other resource borrowed from its caller.

## Import reference

The public modules are organized by responsibility:

- `agora_workbench.data_lake`: records, protocols, policy composition,
  capabilities, and errors
- `agora_workbench.data_lake.catalog`: `CatalogConfig`, `CatalogDB`,
  `CatalogIndexer`, `SearchConfig`, and `SourceConfig`
- `agora_workbench.data_lake.resolvers`: `SearchIndexArtifactResolver`
- `agora_workbench.data_lake.execution`: fetchers, publishers, storage
  credentials, and `DataLakeDataManager`

Use these paths for new code. Imports under
`agora_workbench.code_execution.data_access` are also supported for applications
that already use them.

Azure-backed implementations require the `azure` extra. Vector catalog
operations require the `catalog-vector` extra, while keyword-only SQLite FTS5
catalog operations remain available in the base installation.
