# Public data-lake API

`agora_workbench.data_lake` is the supported public boundary for catalog discovery
and artifact resolution. The namespace contains
backend-neutral records and structural protocols; it does not require a server,
kernel, or execution session.

## Contract overview

The public records keep four concepts separate:

- `ArtifactReference` is the stable logical identity `(source_id, artifact_id)`.
- `StorageLocator` is a physical URI understood by a fetcher or storage provider.
- `ArtifactPresentation` is human-facing metadata.
- `DownloadInfo` is an optional presentation-layer download link and expiry.

Catalog providers implement the async `CatalogProvider` protocol for `search`,
`list`, `get`, and `resolve`. Future mutation contracts will be additive after
content, precondition, and revision semantics are defined.

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

Providers receive the same `RequestContext` on every operation. Its fields are
copied into immutable mappings for propagation, not authorization or cache-key
identity. `SearchRequest.source_ids` and `ListRequest.source_ids` select sources
explicitly; an empty tuple means no source restriction. Pagination cursors are
opaque provider values, callers must not parse them, and page limits cannot exceed
the conservative public maximum of 1000.

## Capabilities and errors

Each `SourceCapabilities` value identifies one `source_id` and its authoritative
`supported_operations`. Caller policy is separate. A gateway or application may
compose effective capabilities by intersecting provider support with its permitted
operations, but the provider protocol neither decides nor reports authorization.
Do not infer support from method presence.

Other stable error categories include `InvalidRequestError`,
`ArtifactNotFoundError`, `UnsupportedOperationError`, `PermissionDeniedError`, and
`BackendUnavailableError`. Unclassified `DataLakeError` instances use `INTERNAL`;
generic failures therefore do not imply a retryable backend outage. Provider
implementations should raise typed errors at the public boundary and may retain
backend exceptions as their causes.

`CatalogProvider` and `ArtifactResolver` are runtime-checkable protocols only for
basic structural discovery. `isinstance()` checks member presence, not signatures,
async behavior, return types, or semantic correctness. Capability values remain
the authority for supported catalog operations.

## Existing catalog and resolver construction

The current SQLite catalog, indexer, configuration models, resolver, fetchers,
publishers, and session data manager are compatibility re-exports rather than
copies. Fetchers and publishers remain defined in their
`agora_workbench.code_execution.data_access` implementation modules:

```python
from agora_workbench.data_lake import (
    CatalogConfig,
    CatalogDB,
    CatalogIndexer,
)

config = CatalogConfig.from_yaml("catalog.yaml")
catalog = CatalogDB("catalog.db")
catalog.open()
indexer = CatalogIndexer(config=config, db=catalog)
```

A resolver remains structural and can be constructed without private subclassing:

```python
from agora_workbench.data_lake import ArtifactResolver


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
```

This resolver shape is intentionally identical to
`agora_workbench.code_execution.data_access.ArtifactResolver`, so existing
custom resolvers and `DataLakeDataManager(artifact_resolver=...)` injection
continue to work.

`DataLakeDataManager` calls `resolve` on every manager cache miss. Implementations
own any backend-result caching they require. They may define optional
`async def aclose(self) -> None`; the manager calls it during cleanup. A resolver
owns and closes clients it creates, but must never close a credential or resource
borrowed from its caller.

## Ownership and lifetime

Catalog indexes and their backing clients are normally process-wide resources:
construct them once, share them across requests, and close them at application
shutdown. `DataLakeDataManager`, its local cache, and its fetcher clients are
per-session resources and must be closed when that session ends.

`ResourceLease` records whether a supplied resource is `OWNED` or `BORROWED`.
The recipient closes owned resources only. In particular, a resolver or manager
must not close a credential borrowed from its caller; it may close clients that
it created with that credential.

## Dependency and import boundary

```text
agora_workbench.data_lake.models/errors/protocols
                    |
                    | lightweight public contracts
                    v
custom providers and catalog-only callers

agora_workbench.data_lake (lazy implementation exports)
                    |
                    +--> code_execution.data_access.catalog
                    +--> code_execution.data_access.artifact_resolvers
                    +--> code_execution.data_access.manager/fetchers/publishers
```

Importing the contract modules does not import execution, session, or cloud
implementation modules. Accessing a concrete compatibility export does not
initialize the execution server or session stack. This boundary permits
implementation dependencies to become optional later without changing the
contracts.

## Compatibility policy

- Imports documented under `agora_workbench.data_lake` are the preferred public API.
- Existing `agora_workbench.code_execution.data_access` imports remain supported.
- Compatibility exports are the same class or protocol objects, not maintained copies.
- Existing resolver behavior, custom fetchers, custom publishers, and injected
  data managers retain their current runtime semantics.
- Future mutation contracts are additive. Other additive record fields and
  protocols may be introduced compatibly. Removing or
  renaming public symbols, changing error categories, or changing protocol method
  signatures requires a normal deprecation cycle.
