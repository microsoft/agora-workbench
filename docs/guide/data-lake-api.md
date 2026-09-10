# Data-lake API

Use the data-lake API to describe artifacts, search a catalog, resolve artifact
IDs to storage locations, and connect those artifacts to code execution.

Choose the import path that matches your task:

| Task | Import from |
| --- | --- |
| Define artifact records or implement a catalog/resolver | `agora_workbench.data_lake` |
| Use the built-in SQLite catalog and indexer | `agora_workbench.data_lake.catalog` |
| Use the Azure AI Search resolver | `agora_workbench.data_lake.resolvers` |
| Configure fetchers, publishers, credentials, or `DataLakeDataManager` | `agora_workbench.data_lake.execution` |

Importing `agora_workbench.data_lake` does not start a server, create an
execution session, or load cloud SDK modules.

## Represent artifacts

The API separates an artifact's identity, storage location, and display
metadata:

- `ArtifactReference` identifies an artifact by `source_id` and `artifact_id`.
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

Providers receive the same `RequestContext` on every operation. Its fields are
copied into immutable mappings so they can be propagated safely. They do not
define authorization or cache identity.

Use `SearchRequest.source_ids` and `ListRequest.source_ids` to restrict an
operation to specific sources. An empty tuple means all sources. Treat
pagination cursors as opaque provider values; callers should not parse them.
Page limits cannot exceed `MAX_PAGE_LIMIT` (1000).

## Capabilities and errors

Each `SourceCapabilities` value reports the operations supported by one
`source_id`. Check these values rather than inferring support from method
presence. Authorization is separate: the application or gateway decides which
supported operations the current caller may use.

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

`ResourceLease` records whether a supplied resource is `OWNED` or `BORROWED`.
Close only owned resources. A resolver or manager may close a client it creates,
but must not close a credential or other resource borrowed from its caller.

## Import reference

The public modules are organized by responsibility:

- `agora_workbench.data_lake`: records, protocols, capabilities, and errors
- `agora_workbench.data_lake.catalog`: `CatalogConfig`, `CatalogDB`,
  `CatalogIndexer`, `SearchConfig`, and `SourceConfig`
- `agora_workbench.data_lake.resolvers`: `SearchIndexArtifactResolver`
- `agora_workbench.data_lake.execution`: fetchers, publishers, storage
  credentials, and `DataLakeDataManager`

Use these paths for new code. Imports under
`agora_workbench.code_execution.data_access` are also supported for applications
that already use them.
