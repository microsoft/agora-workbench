# Data-lake API reference

This page is the detailed reference for transfer behavior, artifact contracts,
custom providers and resolvers, identity, managed writes, and compatibility.

**New to the data-lake API?** Start with
[Add a data catalog to your server](data-lake.md). It takes you from a local
CSV to searchable MCP tools before introducing these extension points.

Most applications configure the built-in catalog and `CatalogIntegration`;
they do not need to implement a provider or call the low-level transfer seam
directly.

Choose the import path that matches your task:

| Task | Import from |
| --- | --- |
| Define artifact records or implement a catalog/resolver | `agora_workbench.data_lake` |
| Use the built-in SQLite catalog and indexer | `agora_workbench.data_lake.catalog` |
| Use manifest-backed provider/resolver adapters | `agora_workbench.data_lake.catalog` / `agora_workbench.data_lake.resolvers` |
| Register, upload, remove, or promote durable artifacts | `agora_workbench.data_lake` / `agora_workbench.data_lake.catalog` |
| Use the Azure AI Search resolver | `agora_workbench.data_lake.resolvers` |
| Configure fetchers, publishers, credentials, or `DataLakeDataManager` | `agora_workbench.data_lake.execution` |

Importing `agora_workbench.data_lake` does not start a server, create an
execution session, or load cloud SDK modules.

For supported administrative commands, runnable examples, Azure Storage
hosting guidance, and operational limits, see
[Data-lake CLI and quickstarts](data-lake-operations.md). For import,
configuration, reference, and rollback changes from 0.2.x, see
[Data-lake migration](data-lake-migration.md).

## Stream artifact bytes safely

`AssetFetcher.fetch()` is explicitly a full-memory convenience. Use
`fetch_to_file()` for files that may be large. The built-in local and Azure
Blob fetchers commit through a sibling partial file, replace the destination
only after successful validation, and remove the partial after size, quota,
timeout, cancellation, checksum, or provider failure.

```python
import asyncio
from pathlib import Path

from agora_workbench.data_lake import RequestContext, TransferOptions
from agora_workbench.data_lake.execution import LocalFileFetcher

cancellation = asyncio.Event()
fetcher = LocalFileFetcher(allowed_roots=["/srv/approved-data"])
result = await fetcher.fetch_to_file_result(
    "file:///srv/approved-data/weather.parquet",
    Path("weather.parquet"),
    options=TransferOptions(
        max_bytes=512 * 1024 * 1024,
        quota_bytes=2 * 1024 * 1024 * 1024,
        timeout_seconds=120,
        expected_sha256="...",  # optional 64-character hex digest
        cancellation_event=cancellation,
    ),
    context=RequestContext(request_id="request-42", caller_id="analyst"),
)
```

The default limit is 1 GiB per transfer, the default end-to-end timeout is
300 seconds, and the default Workbench copy chunk is 1 MiB. Blob downloads
default to four concurrent 4 MiB SDK ranges, so Workbench/provider buffering
is bounded independently of object size (approximately 17 MiB plus SDK and
transport overhead). Operators may tune `MCP_BLOB_MAX_CONCURRENCY`,
`MCP_BLOB_CHUNK_SIZE`, and `MCP_BLOB_MAX_SINGLE_GET`; those values define the
deployment's memory budget.

`TransferResult` contains the byte count, computed SHA-256, elapsed time,
credential-free resource display value, and the exact `RequestContext`.
`TransferOptions.diagnostic_hook` receives start/completion/failure events with
that same context for audit integration. Query strings, fragments, user-info,
and SAS credentials are removed from diagnostics. Hooks must not add raw
credentials themselves. Hook exceptions are logged and ignored: they cannot
turn a committed transfer into a reported failure or replace the primary
transfer error.
When an upstream layer has already replaced URI user-info with the
scheme-less `******` marker, presentation sanitization emits only the
credential-free `host/path`; it does not invent a replacement scheme.

`AssetPublisher.publish()` streams regular files for the built-in local and
Blob publishers. `publish_with_result()` additionally returns the
`TransferResult`; this is the stable byte-transfer seam intended for managed
catalog commits. Set `TransferOptions(create_exclusive=True)` for conditional
object creation. Blob uses an `If-None-Match: *` create precondition; local
publishing uses an atomic create-only link. A successful conditional create
returns `TransferResult.created == True`. Ordinary overwrite mode returns
`None` because every provider cannot reliably distinguish create from replace.
For local conditional creates, successfully linking the validated bytes under
the final name is the commit point; cleanup of the private temporary link is
best-effort and cannot turn that committed result into a reported failure.
Blob publication uploads an immutable disk snapshot, and its byte count and
checksum describe that exact snapshot rather than a second read of a mutable
source. The snapshot is created in the publisher-controlled `staging_dir`
(defaulting below `MCP_ASSET_CACHE_DIR`, or a private working-directory staging
folder), so publishing requires only read access to the source directory.
Secure Blob snapshot staging currently requires POSIX descriptor-relative
filesystem primitives; constructing `BlobPublisher` on other platforms raises
an explicit unsupported-capability error.
`TransferOptions.object_metadata` is copied into Azure object metadata and
returned immutably on `TransferResult`; unsupported providers reject it
explicitly. Credential-bearing metadata keys are rejected, as are URI values
containing user information, query parameters, or fragments. Blob metadata
keys must use Azure's identifier syntax: an ASCII letter or underscore followed
only by ASCII letters, digits, and underscores.

The provider namespace is centralized in `agora_workbench.data_lake.identity`:
`RESERVED_MANIFEST_PATH`, `RESERVED_OPERATIONS_PREFIX`,
`RESERVED_REVISIONS_PREFIX`, and `RESERVED_RECEIPTS_PREFIX`. Ordinary transfer
calls reject all `.agora/` paths. Trusted managed-write code may set
`TransferOptions(allow_reserved=True)` after validating a revision object path
with `validate_managed_revision_path()`. This is intentionally narrow:
authoritative manifest logical artifact paths can never use reserved names,
while physical revision bytes may live only below `.agora/revisions/`.

The managed-storage transfer seam is:

```python
uri, transfer = await blob_publisher.publish_with_result(
    snapshot,
    ".agora/revisions/operation-42/data.bin",
    "",
    options=TransferOptions(
        create_exclusive=True,
        allow_reserved=True,
        object_metadata={"agora_operation_id": "operation-42"},
        max_bytes=...,
        quota_bytes=...,
        timeout_seconds=...,
        cancellation_event=...,
        diagnostic_hook=...,
    ),
    context=request_context,
)
assert transfer.created is True
```

The returned checksum and byte count cover the exact uploaded bytes; context,
timeout, cancellation, quota, diagnostics, metadata, and conditional-create
state cross the same provider call. Publication does **not** register or
reconcile a catalog manifest.
Managed commit/recovery is a separate lifecycle and must not assume that the
catalog layer owns cleanup of a failed conditional create.

The peer `ServerPublisher.publish()` copies the serialized object into an
immutable bounded snapshot, validates size/quota/checksum/cancellation, and
streams its base64 JSON body with backpressure under the remaining timeout.
It marks that backward-compatible JSON body as protocol version 2 so updated
receivers incrementally decode it directly to a bounded temporary file; older
receivers can continue parsing the same JSON envelope through the legacy path.
Updated receivers also incrementally parse absent-version legacy v1 envelopes,
enforcing declared and observed encoded-body limits before bounded base64
decoding; they never materialize the full JSON request body.
The peer protocol does not expose a durable storage locator or conditional
creation result, so `publish_with_result()` still raises
`UnsupportedOperationError` rather than claiming that detailed capability.
Custom fetchers that implement only `fetch_to_file()` remain usable by the
legacy manager, but must implement `fetch_to_file_result()` to advertise the
bounded streaming contract.

### Storage boundaries

- Local reads may be restricted with `allowed_roots`. On POSIX, each path
  component is opened relative to a retained, identity-verified trusted root
  descriptor with
  no-follow semantics, preventing traversal, symlink escape, and the common
  check-then-swap race. Because equivalent primitives are unavailable through
  Python on other platforms, configured `allowed_roots` are explicitly
  unsupported there; unrestricted local reads remain available.
- `LocalFilePublisher(base_dir=...)` treats `base_dir` as its write root and
  creates/opens destination components relative to that root on POSIX. Partial
  files are never exposed as the final name. Other platforms retain legacy
  publishing with resolved-containment and parent-identity checks, but do not
  claim the stronger descriptor-relative guarantee.
- Local catalog scanning requires POSIX descriptor-relative filesystem
  primitives and is rejected when those guarantees are unavailable.
- `BlobFetcher(allowed_locations=[...])` accepts `AzureBlobScope` values or
  supported Azure URI strings. Account, container, and prefix boundaries are
  checked before creating a client or making a request.
- `BlobPublisher` validates its HTTPS account URL, Azure container, and
  optional `prefix` during construction. Blob paths reject dot segments,
  backslashes, encoded separators, malformed percent escapes, and provider
  reserved paths.
- `.agora/manifest.json` and `.agora/operations/` (and the containing
  `.agora/` namespace), plus `.agora/revisions/` and `.agora/receipts/`, are
  reserved for provider metadata. Local and Blob scans prune hidden/dot
  directories and the complete reserved namespace. Ordinary fetch/publish
  operations reject reserved paths.
- Supported Azure locator forms are `az://`, Blob/DFS `https://`, and
  `abfss://`. The default manager rejects arbitrary web URLs. Add a custom
  fetcher explicitly when an operator intends to enable another scheme.

Catalog `StorageLocator` and `resolve()` values remain the internal,
credential-capable locations required by fetchers. Do not sanitize or replace
those values inside a provider. Sanitize only logs, diagnostics, activity
events, download/presentation metadata, and other agent-facing references.

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

## Managed writes

`ManagedCatalogWriter` adds durable mutations to an authoritative manifest.
Use `LocalManagedStorage` for one filesystem root or `BlobManagedStorage` with
a caller-owned container/transfer adapter. Wrap the writer in
`AuthorizedManagedCatalogWriter` so application policy is checked before any
metadata or byte mutation.

```python
from pathlib import Path

from agora_workbench.data_lake import (
    ArtifactMetadata,
    AuthorizedManagedCatalogWriter,
    LocalManagedStorage,
    ManagedCatalogWriter,
    PromoteOutputRequest,
    RequestContext,
)

backend_writer = ManagedCatalogWriter(
    "approved-results",
    LocalManagedStorage("/srv/data/approved"),
)
# `catalog_authorizer` is the application's caller-aware CatalogAuthorizer.
writer = AuthorizedManagedCatalogWriter(backend_writer, catalog_authorizer)
context = RequestContext(caller_id="researcher@example.com")
committed = await writer.promote(
    PromoteOutputRequest(
        operation_id="run-42-result",
        path="results/run-42.csv",
        local_path=Path("/srv/scratch/session-7/result.csv"),
        metadata=ArtifactMetadata(description="Validated run 42 result"),
        session_id="session-7",
        output_name="result.csv",
    ),
    context,
)
# Direct backend access is trusted-only; ordinary callers mutate through `writer`.
manifest = await backend_writer.read_manifest(minimum_generation=committed.generation)
```

Promotion is always explicit. Ordinary `AssetPublisher.publish()` calls and
session output directories remain scratch publication and never become
discoverable merely because bytes were copied. Promotion copies the output to
an immutable managed revision and does not delete or transfer ownership of the
caller/session file.

The four mutations have distinct ownership:

- `register()` requires the caller's SHA-256 and snapshots bytes already inside
  the configured source into a managed immutable revision. The original path
  remains caller-owned and is never modified or deleted. Catalog reads resolve
  the snapshot, so later changes at the external path cannot mutate a retained
  revision.
- `upload()` creates a managed immutable revision from a local file.
- `promote()` is an upload with required session/output provenance.
- `remove()` commits a tombstone before optional garbage collection. Collection
  only deletes revisions marked `managed`, created by the recorded operation,
  and still at the recorded storage version.

Each retained revision stores the provenance of the operation that created it,
and each removal-history entry stores the provenance of its tombstone
operation. The artifact-level provenance remains the latest operation for
compatibility, without replacing the audit history of older revisions.

Every request supplies a stable `operation_id`. An operation intent is
create-exclusive; retries with the same request return the same revision and
committed result, while reuse for different input raises `ConflictError`.
Authorized writers check both source scope and the normalized effective
`ArtifactReference` before creating the intent or transferring bytes.
Revision objects are create-exclusive (`O_EXCL` locally and
`If-None-Match: *` on Blob). Manifest commits use a generation plus an atomic
replacement under the local writer lock, or Blob ETag `If-Match`. Conflicts are
merged and retried only up to `max_conflict_retries`; exhaustion raises
`RetryExhaustedError`. These are ordered, recoverable steps, **not** a
multi-object atomic transaction.

Local writers sharing a root serialize through an advisory `flock`. New files,
replacement manifests, and their containing directories are fsynced before the
lock is released. Readers need no lock because they see either complete
manifest generation. Blob writers use optimistic ETag concurrency. On the
writer host, `read_manifest(minimum_generation=...)` provides read-after-write
for the returned generation. Independent `ManifestCatalogProvider` readers see
the generation after their next successful refresh; deployments must set their
refresh interval and `max_stale_seconds` to the required cross-reader freshness
bound.

Call `reconcile(grace_seconds=...)` after startup or periodically. It creates a
missing receipt when a manifest commit succeeded, and removes an uncommitted
orphan only when the operation record, object ownership metadata, and storage
version prove that the object belongs to that operation and no committed
revision references it. Young operations are deferred. Unsafe cleanup raises
`ReconciliationError` rather than deleting ambiguous bytes.
Active operations hold a durable renewable lease. Reconciliation conditionally
claims an expired operation under the local writer lock or Blob ETag CAS before
cleanup, and writers re-check their lease before manifest commit. A slow active
transfer or manifest CAS therefore cannot be collected based on age alone.
Once revision bytes are complete, the operation enters `commit_ready`;
reconciliation does not delete that revision while a manifest commit remains
possible. After the lease expires, reconciliation conditionally claims the
intent and commits a manifest fence generation. The fence CAS either loses to
the original commit, allowing receipt recovery, or changes the manifest ETag so
the old CAS can no longer succeed; only then may the operation become retryable
or its verified owned orphan be collected. Fence generations are visible
manifest generations, but the fenced operation retains its original generation
precondition only when it equals the recorded pre-fence generation and no
unrelated catalog mutation occurred before the fence was cleared. Remove receipts
persist `cleanup_pending`; retries and reconciliation resume requested garbage
collection. Confirmed absence after a tombstone satisfies deletion; an
ownership/version mismatch remains pending.
Committed tombstone records retain their operation result and exact revision
cleanup set even if a later upload revives the artifact, so receipt recovery
cannot delete the replacement revision or lose the completed removal.

Local transfer staging lives only under `.agora/staging/{operation_id}`.
Entries and staged bytes are fsynced before publication. Reconciliation removes
abandoned staging after the grace period under the writer lock; unlinking a
post-link leftover does not remove the already-published revision hard link.

Managed fields are additive to manifest version 1, but managed storage paths
live in the reserved namespace and cannot be replayed as caller-visible logical
paths. Rollback therefore uses documented forward recovery rather than a lossy
legacy export: stop writers, preserve the complete `.agora` namespace, redeploy
managed-write-capable code, and run `reconcile()`. Do not point an older writer
at the source, guess ownership, or remove `.agora` objects manually.

### Transfer integration boundary

Managed commit/recovery uses a private storage adapter boundary. Its transfer
methods accept the shared `TransferOptions`, enforce exact checksums, use the
shared reserved-path validation, and atomically attach ownership metadata while
preserving the stronger lifecycle state needed for crash recovery. The adapter
keeps Blob prefix composition in exactly one layer and reports success only
after the complete revision is durable.

Use `managed_writer_extension_factory(writer)` as
`CatalogIntegration.capability_extension_factory`. It creates a
session-scoped `AuthorizedManagedCatalogWriter` from the same per-session
authorizer as catalog reads, and `CatalogIntegration.capabilities()` merges its
authorized `WRITE_OPERATIONS`. No second server or session lifecycle is
created.

## Caller-aware policy composition

Each `SourceCapabilities` value reports the operations supported by one
`source_id`; check these values rather than inferring support from method
presence. Provider support is not caller authorization. Compose an
application-defined `CatalogAuthorizer` outside the provider with
`AuthorizedCatalogProvider`; its caller-aware `capabilities(context)` result is
the intersection of provider support and policy.

Search providers may set `CatalogArtifact.score` to a backend-neutral relevance
value. Discovery adapters preserve it as the legacy-compatible `score` payload
field without assigning cross-provider meaning to the number.

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

`CodeExecutionServer` provides the opt-in lifecycle composition through
`CatalogIntegration`. `CatalogIntegration.from_config(...)` owns the generated
SQLite provider and loads it at server startup. Passing
`ResourceLease(provider, ResourceOwnership.BORROWED)` mounts an
application-managed provider without refreshing or closing it by default.
An owned provider must expose `aclose()`, `close()`, or `cleanup()`.
Startup failure and cancellation close owned providers and remove their private
cache. Shutdown closes execution sessions before the shared provider, so one
session cannot invalidate another session's resolver.

Discovery tools use `AuthorizedCatalogProvider` and a session-specific
`RequestContext`; they never expose the legacy raw `query_catalog` SQL surface.
Use `authorizer_factory(SessionContext)` when policy objects hold mutable
caller state. The factory is invoked again on bearer-token rotation, and its
session-owned authorizers are closed after replacement or session teardown.
`get_catalog_capabilities` and
`CodeExecutionServer.get_data_lake_capabilities(session)` expose the effective
read operations after provider support and caller policy are intersected.
Discovery omits executable `load_path` references in `PER_ARTIFACT` mode unless
artifact-level resolve authorization can be established; source-level
`RESOLVE` capability alone is not advertised as proof.
`CatalogIntegration.capability_extension_factory` can attach session-owned,
authorized capability providers without changing the read provider lifecycle.
Their `SourceCapabilities` are merged by source, allowing a later managed
writer adapter to contribute write operations while remaining independently
authorized and cleaned up.

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
