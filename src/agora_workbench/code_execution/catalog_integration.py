"""Opt-in catalog lifecycle and discovery integration for code execution."""

from __future__ import annotations

import base64
import binascii
import asyncio
import inspect
import json
import logging
import shutil
import uuid
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager, nullcontext
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Any, Annotated, cast

from fastmcp import Context
from pydantic import Field

from agora_workbench.data_lake import (
    ArtifactNotFoundError,
    ArtifactReference,
    BackendUnavailableError,
    CatalogArtifact,
    CatalogAuthorizer,
    CatalogOperation,
    CatalogPolicyEnforcer,
    CatalogPolicyMode,
    CatalogProvider,
    DataLakeError,
    DevelopmentAllowAllCatalogAuthorizer,
    ListRequest,
    Page,
    PageRequest,
    PermissionDeniedError,
    RequestContext,
    ResolvedArtifact,
    ResourceLease,
    ResourceOwnership,
    SearchRequest,
    SourceCapabilities,
    stable_source_id,
)
from agora_workbench.data_lake.catalog import CatalogConfig, CatalogDB, CatalogIndexer, DiscoveryMode, SourceConfig
from agora_workbench.data_lake.policy import AuthorizedCatalogProvider
from agora_workbench.data_lake.providers import SQLiteCatalogProvider
from agora_workbench.data_lake.transfer import contains_artifact_locator, safe_artifact_reference

from .data_access.catalog.indexer import ManifestRefreshError
from .sessions.session import SessionContext

LOGGER = logging.getLogger(__name__)

_MAX_TOOL_PAGE_SIZE = 100
_MAX_DOMAIN_SCAN = 1_000
_REFERENCE_PREFIX = "catalog-v1:"
_RESERVED_PAYLOAD_FIELDS = frozenset(
    {
        "id",
        "source_id",
        "current_revision",
        "content_revision",
        "metadata_revision",
        "checksum_sha256",
        "score",
        "description",
        "content_type",
        "size_bytes",
        "load_path",
        "name",
        "storage_uri",
    }
)

AuthorizerFactory = Callable[[SessionContext], CatalogAuthorizer]
CapabilityExtensionFactory = Callable[
    [SessionContext, AuthorizedCatalogProvider, RequestContext],
    object | tuple[object, ...] | list[object] | None,
]


def _effective_source_id(source: SourceConfig) -> str:
    """Match the catalog indexer's public fallback source identity contract."""
    root = str(Path(source.path).resolve()) if source.source_type == "local" else source.path
    return source.source_id or stable_source_id(source.source_type, root)


class _AsyncCleanupTracker:
    """Retain async cleanup started from synchronous lifecycle paths."""

    def __init__(self) -> None:
        self._tasks: dict[asyncio.Task[None], tuple[Callable[[], Any] | None, int]] = {}
        # Cleanup trackers are owned by one session manager event-loop context;
        # this depth only guards nested temporary-loop cleanup on that owner.
        self._synchronous_depth = 0

    @property
    def running_synchronously(self) -> bool:
        """Whether cleanup is currently being driven by a temporary event loop."""
        return self._synchronous_depth > 0

    def schedule(
        self,
        awaitable: Any,
        *,
        retry: Callable[[], Any] | None = None,
        cancellation_retries: int = 1,
    ) -> asyncio.Task[None] | None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            cancelled: asyncio.CancelledError | None = None
            current = awaitable
            remaining_retries = cancellation_retries if retry is not None else 0
            self._synchronous_depth += 1
            try:
                while True:
                    try:
                        loop.run_until_complete(current)
                    except asyncio.CancelledError as exc:
                        cancelled = cancelled or exc
                        if retry is None or remaining_retries <= 0:
                            raise cancelled
                        remaining_retries -= 1
                        current = retry()
                    except Exception as exc:
                        if retry is None or remaining_retries <= 0:
                            if cancelled is not None:
                                cancelled.add_note(f"Additional catalog cleanup failure: {exc!r}")
                                raise cancelled
                            raise
                        remaining_retries -= 1
                        current = retry()
                    else:
                        if cancelled is not None:
                            raise cancelled
                        return None
            finally:
                self._synchronous_depth -= 1
                loop.close()
        task = loop.create_task(awaitable)
        self._tasks[task] = (retry, cancellation_retries if retry is not None else 0)
        task.add_done_callback(self._discard_successful)
        return task

    def discard(self, task: asyncio.Task[None]) -> None:
        """Release ownership after a session binding has drained the task."""
        self._tasks.pop(task, None)

    def _discard_successful(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is None:
            self._tasks.pop(task, None)

    async def drain(self) -> list[Exception]:
        errors: list[Exception] = []
        cancelled: asyncio.CancelledError | None = None
        while self._tasks:
            tasks = tuple(self._tasks)
            try:
                results = await asyncio.gather(
                    *(asyncio.shield(task) for task in tasks),
                    return_exceptions=True,
                )
            except asyncio.CancelledError:
                # The shielded tasks continue running. Keep every task strongly
                # tracked so a later shutdown/drain still waits for completion.
                raise
            for task, result in zip(tasks, results):
                retry, cancellation_retries = self._tasks.pop(task, (None, 0))
                if isinstance(result, asyncio.CancelledError):
                    cancelled = cancelled or result
                    if retry is not None and cancellation_retries > 0:
                        self.schedule(
                            retry(),
                            retry=retry,
                            cancellation_retries=cancellation_retries - 1,
                        )
                elif isinstance(result, Exception):
                    if retry is not None and cancellation_retries > 0:
                        self.schedule(
                            retry(),
                            retry=retry,
                            cancellation_retries=cancellation_retries - 1,
                        )
                    else:
                        errors.append(result)
        if cancelled is not None:
            if errors:
                cancelled.add_note(str(ExceptionGroup("Additional catalog cleanup failures.", errors)))
            raise cancelled
        return errors


async def _close_resources(resources: list[object]) -> None:
    errors: list[Exception] = []
    cancelled: asyncio.CancelledError | None = None
    for resource in tuple(resources):
        close = (
            getattr(resource, "aclose", None) or getattr(resource, "close", None) or getattr(resource, "cleanup", None)
        )
        if not callable(close):
            _remove_resource_identity(resources, resource)
            continue
        try:
            result = close()
            if inspect.isawaitable(result):
                _ = await result
        except asyncio.CancelledError as exc:
            cancelled = cancelled or exc
        except Exception as exc:
            errors.append(exc)
        else:
            _remove_resource_identity(resources, resource)
    if cancelled is not None:
        if errors:
            cancelled.add_note(str(ExceptionGroup("Additional resource cleanup failures.", errors)))
        raise cancelled
    if errors:
        raise ExceptionGroup("Resource cleanup failed.", errors)


def _remove_resource_identity(resources: list[Any], resource: object) -> None:
    for index, candidate in enumerate(resources):
        if candidate is resource:
            resources.pop(index)
            return


@dataclass(frozen=True)
class _PreparedContextRefresh:
    commit: Callable[[], None]
    rollback_resource: object | None = None
    rollback: Callable[[], None] | None = None
    retire_resource: object | None = None

    def __call__(self) -> None:
        self.commit()


class SessionCredential:
    """Adapt a workbench credential provider to the async Azure credential shape."""

    def __init__(self, provider: Any, *, provider_factory: Callable[[str], Any] | None = None):
        self._provider = provider
        self._provider_factory = provider_factory
        self._retired_providers: list[Any] = []
        self._provider_users: dict[int, int] = {}
        self._provider_drained: dict[int, asyncio.Event] = {}
        self._retired_cleanup_tasks: dict[int, asyncio.Task[None]] = {}
        self._provider_retirements: list[_RetiredCredentialProvider] = []
        self._provider_closed = False
        self._closing = False

    async def get_token(self, *scopes: str, **kwargs: object) -> Any:
        del kwargs
        if not scopes:
            raise ValueError("At least one scope is required.")
        provider = self._provider
        provider_id = id(provider)
        event = self._provider_drained.get(provider_id)
        if event is None:
            event = asyncio.Event()
            self._provider_drained[provider_id] = event
        event.clear()
        self._provider_users[provider_id] = self._provider_users.get(provider_id, 0) + 1
        try:
            return await provider.get_token(scopes[0])
        finally:
            remaining = self._provider_users[provider_id] - 1
            if remaining:
                self._provider_users[provider_id] = remaining
            else:
                self._provider_users.pop(provider_id, None)
                self._provider_drained.pop(provider_id, None)
                event.set()

    def prepare_context_refresh(self, context: SessionContext) -> _PreparedContextRefresh:
        """Build a replacement provider and return a non-failing commit callback."""
        if self._provider_factory is None:
            return _PreparedContextRefresh(lambda: None)
        provider = self._provider_factory(context.user_token)
        previous_provider = self._provider
        if provider is previous_provider:
            return _PreparedContextRefresh(lambda: None)
        previous_retirement = next(
            (retirement for retirement in self._provider_retirements if retirement.provider is provider),
            None,
        )
        if previous_retirement is not None and previous_retirement.started:
            raise RuntimeError(
                f"Credential provider cleanup has already started for session {context.session_id}; "
                "return a new provider instance."
            )
        reactivated_index: int | None = None
        previous_retirement_index: int | None = None
        retirement = _RetiredCredentialProvider(self, previous_provider)

        def commit() -> None:
            nonlocal previous_retirement_index, reactivated_index
            if self._closing:
                raise RuntimeError(
                    f"Credential cleanup has started for session {context.session_id}; cannot commit a context refresh."
                )
            reactivated_index = next(
                (index for index, retired in enumerate(self._retired_providers) if retired is provider),
                None,
            )
            if reactivated_index is not None:
                self._retired_providers.pop(reactivated_index)
                if previous_retirement is not None:
                    previous_retirement_index = self._provider_retirements.index(previous_retirement)
                    self._provider_retirements.pop(previous_retirement_index)
            self._retired_providers.append(previous_provider)
            self._provider_retirements.append(retirement)
            self._provider = provider
            self._provider_closed = False

        def rollback() -> None:
            if self._provider is provider:
                self._provider = previous_provider
                for index in range(len(self._retired_providers) - 1, -1, -1):
                    if self._retired_providers[index] is previous_provider:
                        self._retired_providers.pop(index)
                        break
                _remove_resource_identity(self._provider_retirements, retirement)
                if reactivated_index is not None:
                    self._retired_providers.insert(reactivated_index, provider)
                    if previous_retirement is not None:
                        insertion_index = previous_retirement_index if previous_retirement_index is not None else 0
                        self._provider_retirements.insert(insertion_index, previous_retirement)

        return _PreparedContextRefresh(
            commit,
            rollback_resource=None if previous_retirement is not None else provider,
            rollback=rollback,
            retire_resource=retirement,
        )

    async def _close_retired_provider(self, retirement: _RetiredCredentialProvider) -> None:
        """Close a provider only while its retirement ticket remains current."""
        provider = retirement.provider
        provider_id = id(provider)
        if not any(candidate is retirement for candidate in self._provider_retirements):
            return
        if provider is self._provider:
            return
        existing = self._retired_cleanup_tasks.get(provider_id)
        current_task = asyncio.current_task()
        if existing is not None and existing is not current_task:
            await asyncio.shield(existing)
            return
        retirement.started = True
        if existing is None and current_task is not None:
            self._retired_cleanup_tasks[provider_id] = current_task
        try:
            drained = self._provider_drained.get(provider_id)
            if drained is not None:
                await drained.wait()
            close = getattr(provider, "aclose", None) or getattr(provider, "close", None)
            if callable(close):
                result = close()
                if inspect.isawaitable(result):
                    _ = await result
            for index, retired in enumerate(self._retired_providers):
                if retired is provider:
                    self._retired_providers.pop(index)
                    break
            _remove_resource_identity(self._provider_retirements, retirement)
        finally:
            if self._retired_cleanup_tasks.get(provider_id) is current_task:
                self._retired_cleanup_tasks.pop(provider_id, None)

    async def close(self) -> None:
        self._closing = True
        errors: list[Exception] = []
        cancelled: asyncio.CancelledError | None = None
        providers = tuple((provider, False) for provider in self._retired_providers)
        if not self._provider_closed:
            providers += ((self._provider, True),)
        for provider, is_current in providers:
            try:
                if is_current:
                    drained = self._provider_drained.get(id(provider))
                    if drained is not None:
                        await drained.wait()
                    close = getattr(provider, "aclose", None) or getattr(provider, "close", None)
                    if callable(close):
                        result = close()
                        if inspect.isawaitable(result):
                            _ = await result
                else:
                    retirement = next(
                        (candidate for candidate in self._provider_retirements if candidate.provider is provider),
                        None,
                    )
                    if retirement is not None:
                        await self._close_retired_provider(retirement)
            except asyncio.CancelledError as exc:
                cancelled = cancelled or exc
            except Exception as exc:
                errors.append(exc)
            else:
                if is_current:
                    self._provider_closed = True
                else:
                    for index, retired in enumerate(self._retired_providers):
                        if retired is provider:
                            self._retired_providers.pop(index)
                            break
        if cancelled is not None:
            if errors:
                cancelled.add_note(str(ExceptionGroup("Additional credential cleanup failures.", errors)))
            raise cancelled
        if errors:
            raise ExceptionGroup("Session credential cleanup failed.", errors)

    async def __aenter__(self) -> "SessionCredential":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        del exc_type, exc_value, traceback
        await self.close()


@dataclass(eq=False)
class _RetiredCredentialProvider:
    credential: SessionCredential
    provider: Any
    started: bool = False

    async def aclose(self) -> None:
        await self.credential._close_retired_provider(self)


class _ConfiguredCatalogProvider(SQLiteCatalogProvider):
    """Lifecycle wrapper for scan, manifest, or mixed configured catalogs."""

    def __init__(self, config: CatalogConfig, *, db_path: str | Path = ":memory:", credential_provider: Any = None):
        self._config = config
        self._db_owned = CatalogDB(db_path, vec_dimensions=config.search.embedding_dimensions)
        self._closed = False
        self._embedding_closed = False
        self._db_closed = False
        self._lifecycle_lock = asyncio.Lock()
        self._active_reads = 0
        self._reads_drained = asyncio.Event()
        self._reads_drained.set()
        try:
            self._db_owned.open()
            self._indexer = CatalogIndexer(config, self._db_owned, credential_provider=credential_provider)
            self._configured_source_ids = tuple(_effective_source_id(source) for source in config.sources)
            self._source_stale_limits = {
                _effective_source_id(source): (
                    source.max_stale_seconds if source.max_stale_seconds is not None else 300.0
                )
                for source in config.sources
            }
            self._manifest_source_ids = {
                _effective_source_id(source) for source in config.sources if source.discovery is DiscoveryMode.MANIFEST
            }
            self._failed_manifest_source_ids: set[str] = set()
            super().__init__(
                self._db_owned,
                self._configured_source_ids,
                query_embedder=self._embed_query,
                hybrid_alpha=config.search.hybrid_alpha,
            )
        except BaseException:
            self._db_owned.close()
            self._closed = True
            raise

    async def load(self) -> int:
        async with self._lifecycle_lock:
            await self._reads_drained.wait()
            return await self._load_unlocked()

    async def _load_unlocked(self) -> int:
        if self._closed:
            raise RuntimeError("Catalog provider is closed.")
        try:
            indexed = await self._indexer.index()
        except ManifestRefreshError as exc:
            self._failed_manifest_source_ids = set(exc.errors)
            states = {state.source_id: state for state in self._db_owned.list_source_refresh_states()}
            unavailable = self._unavailable_sources(states, failed_source_ids=self._failed_manifest_source_ids)
            if unavailable:
                raise RuntimeError(f"Catalog sources are not ready: {', '.join(sorted(unavailable))}") from exc
            return sum(state.artifact_count or 0 for state in states.values())
        states = {state.source_id: state for state in self._db_owned.list_source_refresh_states()}
        failed_source_ids = {
            source_id
            for source_id, state in states.items()
            if source_id in self._manifest_source_ids and state.status != "success"
        }
        self._failed_manifest_source_ids = failed_source_ids
        unavailable = self._unavailable_sources(states, failed_source_ids=failed_source_ids)
        if unavailable:
            raise RuntimeError(f"Catalog sources are not ready: {', '.join(sorted(unavailable))}")
        return indexed

    def _unavailable_sources(self, states: Mapping[str, Any], *, failed_source_ids: set[str]) -> list[str]:
        now = datetime.now(timezone.utc)
        unavailable = []
        for source_id in self._configured_source_ids:
            state = states.get(source_id)
            if (
                state is None
                or state.successful_generation < 1
                or (source_id in self._manifest_source_ids and state.manifest_generation is None)
            ):
                unavailable.append(source_id)
                continue
            if source_id not in failed_source_ids:
                continue
            if state.last_success_at is None:
                unavailable.append(source_id)
                continue
            success_at = datetime.fromisoformat(state.last_success_at.replace("Z", "+00:00"))
            if success_at.tzinfo is None:
                success_at = success_at.replace(tzinfo=timezone.utc)
            if (now - success_at.astimezone(timezone.utc)).total_seconds() > self._source_stale_limits[source_id]:
                unavailable.append(source_id)
        return unavailable

    def _require_ready(self) -> None:
        if self._closed:
            raise BackendUnavailableError("Catalog provider is closed.", operation="catalog")
        states = {state.source_id: state for state in self._db_owned.list_source_refresh_states()}
        unavailable = self._unavailable_sources(states, failed_source_ids=self._failed_manifest_source_ids)
        if unavailable:
            raise BackendUnavailableError(
                f"Catalog sources are not ready: {', '.join(sorted(unavailable))}",
                operation="catalog",
            )

    @asynccontextmanager
    async def _read_operation(self) -> AsyncIterator[None]:
        async with self._lifecycle_lock:
            self._require_ready()
            self._active_reads += 1
            self._reads_drained.clear()
        try:
            yield
        finally:
            # Writers hold the lifecycle lock while waiting for this lock-free,
            # non-suspending transition to signal that reads have drained.
            self._active_reads -= 1
            if self._active_reads == 0:
                self._reads_drained.set()

    async def capabilities(self) -> tuple[SourceCapabilities, ...]:
        async with self._read_operation():
            return await super().capabilities()

    async def search(self, request: SearchRequest, context: RequestContext) -> Page[CatalogArtifact]:
        async with self._read_operation():
            return await super().search(request, context)

    async def list(self, request: ListRequest, context: RequestContext) -> Page[CatalogArtifact]:
        async with self._read_operation():
            return await super().list(request, context)

    async def get(self, reference: ArtifactReference, context: RequestContext) -> CatalogArtifact:
        async with self._read_operation():
            return await super().get(reference, context)

    async def resolve(self, reference: ArtifactReference, context: RequestContext) -> ResolvedArtifact:
        async with self._read_operation():
            artifact = await SQLiteCatalogProvider.get(self, reference, context)
            if artifact.locator is None:
                raise ArtifactNotFoundError(
                    "Catalog artifact has no storage locator.",
                    resource_id=reference.artifact_id,
                    operation="resolve",
                )
            return ResolvedArtifact(reference=artifact.reference, locator=artifact.locator)

    async def _embed_query(self, query: str) -> list[float] | None:
        provider = self._indexer.embedding_provider
        if provider is None:
            return None
        embeddings = await provider.embed([query])
        return embeddings[0] if embeddings else None

    async def aclose(self) -> None:
        async with self._lifecycle_lock:
            await self._reads_drained.wait()
            if self._embedding_closed and self._db_closed:
                return
            errors: list[Exception] = []
            cancelled: asyncio.CancelledError | None = None
            self._closed = True
            if not self._embedding_closed:
                try:
                    embedding_provider = vars(self._indexer).get("_embedding_provider")
                    close = getattr(embedding_provider, "aclose", None) or getattr(embedding_provider, "close", None)
                    if callable(close):
                        result = close()
                        if inspect.isawaitable(result):
                            _ = await result
                    self._embedding_closed = True
                except asyncio.CancelledError as exc:
                    cancelled = exc
                except Exception as exc:
                    errors.append(exc)
            if not self._db_closed:
                try:
                    self._db_owned.close()
                    self._db_closed = True
                except Exception as exc:
                    errors.append(exc)
            if cancelled is not None:
                if errors:
                    cancelled.add_note(str(ExceptionGroup("Additional configured catalog close failures.", errors)))
                raise cancelled
            if errors:
                raise ExceptionGroup("Configured catalog close failed.", errors)


def _encode_reference(reference: ArtifactReference) -> str:
    payload = json.dumps(
        {
            "artifact_id": reference.artifact_id,
            "source_id": reference.source_id,
            "revision": reference.revision,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    encoded = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"{_REFERENCE_PREFIX}{encoded}"


def _decode_reference(value: str) -> ArtifactReference:
    if not value.startswith(_REFERENCE_PREFIX):
        raise ValueError("Catalog artifact reference is invalid.")
    encoded = value[len(_REFERENCE_PREFIX) :]
    try:
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode())
        if not isinstance(payload, Mapping):
            raise ValueError
        artifact_id = payload["artifact_id"]
        source_id = payload["source_id"]
        revision = payload.get("revision")
        if not isinstance(artifact_id, str) or not artifact_id:
            raise ValueError
        if not isinstance(source_id, str) or not source_id:
            raise ValueError
        if revision is not None and (not isinstance(revision, int) or isinstance(revision, bool) or revision <= 0):
            raise ValueError
        return ArtifactReference(
            artifact_id=artifact_id,
            source_id=source_id,
            revision=revision,
        )
    except (binascii.Error, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Catalog artifact reference is invalid.") from exc


class CatalogSessionResolver:
    """Resolve execution-safe references through one caller-scoped catalog."""

    def __init__(self, catalog: AuthorizedCatalogProvider, context: RequestContext):
        self._catalog = catalog
        self._context = context
        self._snapshot: Callable[[], CatalogSessionView] | None = None
        self._closed = False
        self._request_snapshot: ContextVar[CatalogSessionView | None] = ContextVar(
            f"catalog-session-view-{id(self)}", default=None
        )

    @property
    def unavailable_reason(self) -> str | None:
        return "The catalog resolver has been closed." if self._closed else None

    async def resolve(self, artifact_id: str) -> str:
        if self._closed:
            raise ValueError(self.unavailable_reason)
        current = self._request_snapshot.get()
        owns_snapshot = current is None
        if current is None and self._snapshot is not None:
            current = self._snapshot()
        try:
            catalog = current.catalog if current is not None else self._catalog
            context = current.context if current is not None else self._context
            resolved = await catalog.resolve(_decode_reference(artifact_id), context)
            return resolved.locator.uri
        finally:
            if owns_snapshot and current is not None:
                current.close()

    @contextmanager
    def bind_request_snapshot(self) -> Iterator[None]:
        """Keep resolution on this request's authorization snapshot."""
        current = self._snapshot() if self._snapshot is not None else None
        token: Token[CatalogSessionView | None] | None = None
        if current is not None:
            token = self._request_snapshot.set(current)
        try:
            yield
        finally:
            if token is not None:
                self._request_snapshot.reset(token)
            if current is not None:
                current.close()

    @contextmanager
    def suspend_request_snapshot(self) -> Iterator[None]:
        """Prevent child tasks from inheriting this request's snapshot."""
        token = self._request_snapshot.set(None)
        try:
            yield
        finally:
            self._request_snapshot.reset(token)

    async def aclose(self) -> None:
        self._closed = True

    def close(self) -> None:
        """Close a resolver from synchronous session teardown."""
        self._closed = True


@dataclass
class CatalogSessionBinding:
    """Caller-scoped policy, context, and execution resolver."""

    catalog: AuthorizedCatalogProvider
    context: RequestContext
    resolver: CatalogSessionResolver
    execution_references: bool
    capability_extensions: tuple[object, ...] = ()
    cleanup_tracker: _AsyncCleanupTracker | None = None
    authorizer_factory: AuthorizerFactory | None = None
    owned_authorizer: CatalogAuthorizer | None = None
    context_refreshers: list[Callable[[SessionContext], Callable[[], None] | _PreparedContextRefresh]] | None = None
    capability_extension_factory: CapabilityExtensionFactory | None = None
    provider: CatalogProvider | None = None
    policy_mode: CatalogPolicyMode = CatalogPolicyMode.HOMOGENEOUS_SOURCE
    per_artifact_enforcer: CatalogPolicyEnforcer | None = None
    _closed: bool = False
    _active_snapshots: int = 0
    _snapshots_drained: asyncio.Event = field(default_factory=asyncio.Event)
    _deferred_resources: list[object] = field(default_factory=list)
    _resolver_closed: bool = False
    _pending_cleanup_resources: list[object] | None = None
    _scheduled_cleanup_resources: list[object] = field(default_factory=list)
    _scheduled_cleanup_tasks: dict[asyncio.Task[None], object] = field(default_factory=dict)
    # Keep retired resources strongly referenced for the binding lifetime so the
    # exact closed object cannot be returned by a later factory refresh.
    _retirement_started_resources: dict[int, object] = field(default_factory=dict)
    _retired_resources: dict[int, object] = field(default_factory=dict)
    _cleanup_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        self._snapshots_drained.set()

    def snapshot(self) -> "CatalogSessionView":
        """Capture one immutable authorization view for a complete operation."""
        if self._closed:
            raise RuntimeError("Catalog session binding is closed.")
        self._active_snapshots += 1
        self._snapshots_drained.clear()
        return CatalogSessionView(
            self.catalog,
            self.context,
            self.execution_references,
            self.capability_extensions,
            self._release_snapshot,
        )

    def _release_snapshot(self) -> None:
        self._active_snapshots -= 1
        if self._active_snapshots != 0:
            return
        self._snapshots_drained.set()
        if not self._closed:
            for resource in self._deferred_resources:
                self._schedule_resource_cleanup(resource)
            self._deferred_resources.clear()

    def refresh_context(self, context: SessionContext) -> None:
        """Refresh authorization inputs when a transport session receives a new token."""
        if self._closed:
            raise RuntimeError("Catalog session binding is closed.")
        request_context = _request_context(context)
        authorizer = self.owned_authorizer
        catalog = self.catalog
        extensions = self.capability_extensions
        prepared_refreshes: list[Callable[[], None] | _PreparedContextRefresh] = []
        try:
            if self.authorizer_factory is not None:
                authorizer = self.authorizer_factory(context)
                assert self.provider is not None
                catalog = AuthorizedCatalogProvider(
                    self.provider,
                    authorizer,
                    mode=self.policy_mode,
                    per_artifact_enforcer=self.per_artifact_enforcer,
                )
            if self.capability_extension_factory is not None:
                created = self.capability_extension_factory(context, catalog, request_context)
                if created is None:
                    extensions = ()
                elif isinstance(created, (tuple, list)):
                    extensions = tuple(created)
                else:
                    extensions = (created,)
            for resource in (*extensions, authorizer):
                if resource is not None and (
                    id(resource) in self._retirement_started_resources or id(resource) in self._retired_resources
                ):
                    raise RuntimeError("Catalog refresh cannot reactivate a resource whose cleanup has started.")
            for refresher in self.context_refreshers or ():
                prepared_refreshes.append(refresher(context))
        except BaseException:
            current_extension_ids = {id(extension) for extension in self.capability_extensions}
            for extension in extensions:
                if (
                    id(extension) not in current_extension_ids
                    and id(extension) not in self._retirement_started_resources
                    and id(extension) not in self._retired_resources
                ):
                    self._schedule_resource_cleanup(extension)
            for prepared in prepared_refreshes:
                if isinstance(prepared, _PreparedContextRefresh) and prepared.rollback_resource is not None:
                    self._schedule_resource_cleanup(prepared.rollback_resource)
            if (
                authorizer is not None
                and authorizer is not self.owned_authorizer
                and id(authorizer) not in self._retirement_started_resources
                and id(authorizer) not in self._retired_resources
            ):
                self._schedule_resource_cleanup(authorizer)
            raise

        previous_authorizer = self.owned_authorizer
        committed_refreshes: list[_PreparedContextRefresh] = []
        try:
            for prepared in prepared_refreshes:
                if isinstance(prepared, _PreparedContextRefresh):
                    committed_refreshes.append(prepared)
                prepared()
        except BaseException as commit_error:
            rollback_errors: list[Exception] = []
            for prepared in reversed(committed_refreshes):
                if prepared.rollback is not None:
                    try:
                        prepared.rollback()
                    except Exception as exc:
                        rollback_errors.append(exc)
            rollback_resources: list[object] = []
            seen_resources: set[int] = set()
            for prepared in prepared_refreshes:
                if (
                    isinstance(prepared, _PreparedContextRefresh)
                    and prepared.rollback_resource is not None
                    and id(prepared.rollback_resource) not in seen_resources
                ):
                    seen_resources.add(id(prepared.rollback_resource))
                    rollback_resources.append(prepared.rollback_resource)
            for resource in rollback_resources:
                self._schedule_resource_cleanup(resource)
            current_extension_ids = {id(extension) for extension in self.capability_extensions}
            for extension in extensions:
                if id(extension) not in current_extension_ids:
                    self._schedule_resource_cleanup(extension)
            if authorizer is not None and authorizer is not self.owned_authorizer:
                self._schedule_resource_cleanup(authorizer)
            if rollback_errors:
                commit_error.add_note(str(ExceptionGroup("Context refresh rollback failed.", rollback_errors)))
            raise
        self.catalog = catalog
        self.resolver._catalog = catalog
        self.owned_authorizer = authorizer
        self.context = request_context
        self.resolver._context = request_context
        previous_extensions = self.capability_extensions
        self.capability_extensions = extensions
        current_extension_ids = {id(extension) for extension in extensions}
        current_resource_ids = set(current_extension_ids)
        if authorizer is not None:
            current_resource_ids.add(id(authorizer))
        self._deferred_resources = [
            resource for resource in self._deferred_resources if id(resource) not in current_resource_ids
        ]
        for resource in (*extensions, authorizer):
            if resource is not None:
                self._cancel_scheduled_resource_cleanup(resource)
        for extension in previous_extensions:
            if id(extension) in current_extension_ids:
                continue
            if self._active_snapshots:
                self._deferred_resources.append(extension)
            else:
                self._schedule_resource_cleanup(extension)
        if previous_authorizer is not None and previous_authorizer is not authorizer:
            if self._active_snapshots:
                self._deferred_resources.append(previous_authorizer)
            else:
                self._schedule_resource_cleanup(previous_authorizer)
        for prepared in prepared_refreshes:
            if isinstance(prepared, _PreparedContextRefresh) and prepared.retire_resource is not None:
                self._schedule_resource_cleanup(prepared.retire_resource)

    def add_context_refresher(
        self,
        refresher: Callable[[SessionContext], Callable[[], None] | _PreparedContextRefresh],
    ) -> None:
        """Register a side-effect-free preparation step for token rebinding."""
        if self.context_refreshers is None:
            self.context_refreshers = []
        self.context_refreshers.append(refresher)

    def _complete_scheduled_resource_cleanup(self, resource: object) -> None:
        _remove_resource_identity(self._scheduled_cleanup_resources, resource)
        retired = self._retirement_started_resources.pop(id(resource), resource)
        self._retired_resources[id(resource)] = retired
        for task, scheduled_resource in tuple(self._scheduled_cleanup_tasks.items()):
            if scheduled_resource is resource:
                self._scheduled_cleanup_tasks.pop(task, None)

    def _track_scheduled_resource(self, resource: object) -> bool:
        if any(existing is resource for existing in self._scheduled_cleanup_resources):
            return False
        self._scheduled_cleanup_resources.append(resource)
        return True

    async def _close_tracked_resource(self, resource: object, *, retry: bool) -> None:
        """Close one retired resource while preserving retry/cancellation state.

        ``retry=True`` gives the resource one immediate retry for transient
        cleanup failure. Use ``retry=False`` when this is already a handoff from
        a broader cleanup retry path, so cancellation/error reporting remains
        bounded and owned by the caller.
        """
        self._retirement_started_resources[id(resource)] = resource
        pending_resources = [resource]
        cancelled: asyncio.CancelledError | None = None
        last_error: Exception | None = None
        for attempt in range(2 if retry else 1):
            try:
                await _close_resources(pending_resources)
            except asyncio.CancelledError as exc:
                cancelled = cancelled or exc
            except Exception as exc:
                last_error = exc
            if not pending_resources:
                self._complete_scheduled_resource_cleanup(resource)
                break
            if retry and attempt == 0:
                continue
            if cancelled is not None:
                if last_error is not None:
                    cancelled.add_note(f"Additional catalog cleanup failure: {last_error!r}")
                raise cancelled
            if last_error is not None:
                raise last_error
        if cancelled is not None:
            raise cancelled

    def _schedule_resource_cleanup(self, resource: object, *, retry: bool = True) -> asyncio.Task[None] | None:
        if self.cleanup_tracker is None:
            raise RuntimeError("Catalog session binding has no cleanup tracker.")
        if not self._track_scheduled_resource(resource):
            return None

        async def cleanup() -> None:
            await self._close_tracked_resource(resource, retry=retry)

        task = self.cleanup_tracker.schedule(cleanup())
        if task is None:
            return None
        self._scheduled_cleanup_tasks[task] = resource

        def cleanup_finished(completed: asyncio.Task[None]) -> None:
            if completed.cancelled():
                return
            try:
                error = completed.exception()
            except asyncio.CancelledError:
                return
            if error is None:
                self._complete_scheduled_resource_cleanup(resource)

        task.add_done_callback(cleanup_finished)
        return task

    def schedule_resource_cleanup(self, resource: object) -> None:
        """Schedule cleanup for a resource that should be retired asynchronously."""
        self._schedule_resource_cleanup(resource)

    def _cancel_scheduled_resource_cleanup(self, resource: object) -> None:
        """Prevent a resource that became current again from being closed as retired."""
        for task, scheduled_resource in tuple(self._scheduled_cleanup_tasks.items()):
            if scheduled_resource is resource:
                self._scheduled_cleanup_tasks.pop(task, None)
                if self.cleanup_tracker is not None:
                    self.cleanup_tracker.discard(task)
                task.add_done_callback(self._consume_cancelled_resource_cleanup)
                task.cancel()
        _remove_resource_identity(self._scheduled_cleanup_resources, resource)

    @staticmethod
    def _consume_cancelled_resource_cleanup(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            LOGGER.error(
                "Cancelled catalog resource cleanup failed with %s: %s",
                type(error).__name__,
                _sanitize_error_message(str(error)),
            )

    async def aclose(self) -> None:
        """Close session-owned extension resources, never the shared read provider."""
        async with self._cleanup_lock:
            await self._aclose_once()

    async def _aclose_once(self) -> None:
        if (
            self._closed
            and self._resolver_closed
            and self._pending_cleanup_resources == []
            and not self._scheduled_cleanup_tasks
            and not self._scheduled_cleanup_resources
        ):
            return
        self._closed = True
        errors: list[Exception] = []
        cancelled: asyncio.CancelledError | None = None
        try:
            await self._snapshots_drained.wait()
        except asyncio.CancelledError:
            self._closed = False
            raise
        if self._scheduled_cleanup_tasks:
            tasks = tuple(self._scheduled_cleanup_tasks)
            try:
                results = await asyncio.gather(
                    *(asyncio.shield(task) for task in tasks),
                    return_exceptions=True,
                )
            except asyncio.CancelledError:
                self._closed = False
                raise
            for task, result in zip(tasks, results):
                resource = self._scheduled_cleanup_tasks.pop(task, None)
                if self.cleanup_tracker is not None:
                    self.cleanup_tracker.discard(task)
                if resource is not None and result is None:
                    _remove_resource_identity(self._scheduled_cleanup_resources, resource)
                elif resource is not None and self._pending_cleanup_resources is not None:
                    _remove_resource_identity(self._scheduled_cleanup_resources, resource)
                    if not any(pending is resource for pending in self._pending_cleanup_resources):
                        self._pending_cleanup_resources.append(resource)
        try:
            if not self._resolver_closed:
                await self.resolver.aclose()
                self._resolver_closed = True
        except asyncio.CancelledError as exc:
            cancelled = exc
        except Exception as exc:
            errors.append(exc)
        if self._pending_cleanup_resources is None:
            resources = (
                *self._scheduled_cleanup_resources,
                *self._deferred_resources,
                *self.capability_extensions,
                self.owned_authorizer,
            )
            seen: set[int] = set()
            self._pending_cleanup_resources = []
            for resource in (resource for resource in resources if resource is not None):
                if id(resource) not in seen:
                    seen.add(id(resource))
                    self._pending_cleanup_resources.append(resource)
            self._scheduled_cleanup_resources.clear()
        for extension in tuple(self._pending_cleanup_resources):
            close = (
                getattr(extension, "aclose", None)
                or getattr(extension, "close", None)
                or getattr(extension, "cleanup", None)
            )
            if callable(close):
                try:
                    result = close()
                    if inspect.isawaitable(result):
                        _ = await result
                except asyncio.CancelledError as exc:
                    cancelled = cancelled or exc
                except Exception as exc:
                    errors.append(exc)
                else:
                    _remove_resource_identity(self._pending_cleanup_resources, extension)
            else:
                _remove_resource_identity(self._pending_cleanup_resources, extension)
        if self._pending_cleanup_resources:
            for resource in tuple(self._pending_cleanup_resources):
                task: asyncio.Task[None] | None = None
                try:
                    if self.cleanup_tracker is not None and not self.cleanup_tracker.running_synchronously:
                        task = self._schedule_resource_cleanup(resource, retry=False)
                        if task is not None:
                            await asyncio.shield(task)
                    else:
                        self._track_scheduled_resource(resource)
                        await self._close_tracked_resource(resource, retry=False)
                except asyncio.CancelledError as exc:
                    cancelled = cancelled or exc
                except Exception as exc:
                    errors.append(exc)
                finally:
                    if task is not None and task.done():
                        self._scheduled_cleanup_tasks.pop(task, None)
                        if self.cleanup_tracker is not None:
                            self.cleanup_tracker.discard(task)
                    elif task is not None:
                        # The shielded cleanup is still running after caller
                        # cancellation; keep both registries owning it so a
                        # later shutdown drain observes completion.
                        pass
                    _remove_resource_identity(self._pending_cleanup_resources, resource)
        if cancelled is not None:
            if errors:
                cancelled.add_note(str(ExceptionGroup("Additional catalog session cleanup failures.", errors)))
            raise cancelled
        if errors:
            raise ExceptionGroup("Catalog session binding cleanup failed.", errors)
        self._deferred_resources.clear()
        self.capability_extensions = ()
        self.owned_authorizer = None

    def cleanup(self) -> None:
        """Schedule complete async cleanup from synchronous lifecycle paths."""
        if (
            self._closed
            and self._resolver_closed
            and self._pending_cleanup_resources == []
            and not self._scheduled_cleanup_tasks
            and not self._scheduled_cleanup_resources
        ):
            return
        if self.cleanup_tracker is None:
            raise RuntimeError("Catalog session binding has no cleanup tracker.")
        self.cleanup_tracker.schedule(self.aclose(), retry=self.aclose)


@dataclass(frozen=True)
class CatalogSessionView:
    """Immutable per-request view of a caller's catalog binding."""

    catalog: AuthorizedCatalogProvider
    context: RequestContext
    execution_references: bool
    capability_extensions: tuple[object, ...]
    _release: Callable[[], None] | None = None

    def close(self) -> None:
        """Release resources retained for this request snapshot."""
        release = self._release
        object.__setattr__(self, "_release", None)
        if release is not None:
            release()


class CatalogIntegration:
    """Compose a process-wide catalog with caller-scoped execution sessions.

    Use :meth:`from_config` when the server should construct, load, and own the
    catalog. Use the constructor with a :class:`ResourceLease` to mount an
    application-created provider; borrowed providers are neither loaded nor
    closed unless explicitly requested.
    """

    def __init__(
        self,
        provider: ResourceLease[CatalogProvider],
        *,
        authorizer: CatalogAuthorizer | None = None,
        authorizer_factory: AuthorizerFactory | None = None,
        policy_mode: CatalogPolicyMode = CatalogPolicyMode.HOMOGENEOUS_SOURCE,
        per_artifact_enforcer: CatalogPolicyEnforcer | None = None,
        load_on_startup: bool | None = None,
        capability_extension_factory: CapabilityExtensionFactory | None = None,
    ) -> None:
        if (authorizer is None) == (authorizer_factory is None):
            raise ValueError("Configure exactly one of authorizer or authorizer_factory.")
        self._provider_lease = provider
        self._authorizer = authorizer
        self._authorizer_factory = authorizer_factory
        self._policy_mode = policy_mode
        self._per_artifact_enforcer = per_artifact_enforcer
        self._load_on_startup = provider.should_close if load_on_startup is None else load_on_startup
        self._capability_extension_factory = capability_extension_factory
        self._started = False
        self._private_cache_directory: Path | None = None
        self._cleanup_tracker = _AsyncCleanupTracker()
        self._provider_closed = False
        self._provider_close_task: asyncio.Task[None] | None = None
        self._shutdown_task: asyncio.Task[None] | None = None

    @classmethod
    def from_config(
        cls,
        config: CatalogConfig,
        *,
        authorizer: CatalogAuthorizer | None = None,
        authorizer_factory: AuthorizerFactory | None = None,
        policy_mode: CatalogPolicyMode = CatalogPolicyMode.HOMOGENEOUS_SOURCE,
        per_artifact_enforcer: CatalogPolicyEnforcer | None = None,
        capability_extension_factory: CapabilityExtensionFactory | None = None,
        db_path: str | Path | None = None,
        credential_provider: Any = None,
    ) -> "CatalogIntegration":
        """Create an owned catalog that is loaded during server startup."""
        if not config.sources:
            raise ValueError("Catalog integration requires at least one source.")
        if (authorizer is None) == (authorizer_factory is None):
            raise ValueError("Configure exactly one of authorizer or authorizer_factory.")
        private_cache_directory: Path | None = None
        if db_path is None:
            private_cache_directory = Path.home() / ".cache" / "agora-workbench" / "catalogs" / uuid.uuid4().hex
            private_cache_directory.mkdir(mode=0o700, parents=True)
            db_path = private_cache_directory / "catalog.db"
        try:
            provider = _ConfiguredCatalogProvider(config, db_path=db_path, credential_provider=credential_provider)
        except BaseException:
            if private_cache_directory is not None:
                shutil.rmtree(private_cache_directory, ignore_errors=True)
            raise
        integration = cls(
            ResourceLease(provider, ResourceOwnership.OWNED),
            authorizer=authorizer,
            authorizer_factory=authorizer_factory,
            policy_mode=policy_mode,
            per_artifact_enforcer=per_artifact_enforcer,
            capability_extension_factory=capability_extension_factory,
            load_on_startup=True,
        )
        integration._private_cache_directory = private_cache_directory
        return integration

    @classmethod
    def development_from_config(
        cls,
        config: CatalogConfig,
        **kwargs: Any,
    ) -> "CatalogIntegration":
        """Create an explicitly allow-all local-development integration."""
        return cls.from_config(config, authorizer=DevelopmentAllowAllCatalogAuthorizer(), **kwargs)

    @property
    def provider(self) -> CatalogProvider:
        return self._provider_lease.resource

    async def startup(self) -> None:
        """Load owned catalog state and fail atomically on error or cancellation."""
        if self._started:
            return
        try:
            if self._load_on_startup:
                load = getattr(self.provider, "load", None)
                if not callable(load):
                    raise TypeError("Catalog load_on_startup requires a provider with load().")
                result = load()
                if inspect.isawaitable(result):
                    _ = await result
            await self.provider.capabilities()
            self._started = True
        except BaseException as startup_error:
            close_error: BaseException | None = None
            try:
                if self._provider_lease.should_close:
                    close_task = asyncio.create_task(self._close_provider())
                    try:
                        await asyncio.shield(close_task)
                    except asyncio.CancelledError as exc:
                        close_error = exc
                        if close_task.cancelled():
                            close_task = asyncio.create_task(self._close_provider())
                        try:
                            await asyncio.shield(close_task)
                        except BaseException as retry_error:
                            close_error = retry_error
            except BaseException as exc:
                close_error = exc
            finally:
                if self._provider_closed:
                    try:
                        self._cleanup_private_cache_directory()
                    except Exception as cache_cleanup_error:
                        startup_error.add_note(
                            f"Catalog startup cache cleanup also failed: {type(cache_cleanup_error).__name__}"
                        )
            if close_error is not None:
                startup_error.add_note(f"Catalog startup rollback also failed: {close_error!r}")
            raise

    async def shutdown(self) -> None:
        """Close only resources owned by this integration."""
        task = self._shutdown_task
        if task is None:
            task = asyncio.create_task(self._shutdown_once())
            self._shutdown_task = task
        cancelled: asyncio.CancelledError | None = None
        try:
            while True:
                try:
                    await asyncio.shield(task)
                    break
                except asyncio.CancelledError as exc:
                    cancelled = cancelled or exc
                    if task.done():
                        break
        finally:
            if task.done() and self._shutdown_task is task:
                self._shutdown_task = None
        if cancelled is not None:
            if not task.cancelled():
                try:
                    task.result()
                except Exception as error:
                    cancelled.add_note(f"Catalog shutdown also failed: {error!r}")
            raise cancelled

    async def _shutdown_once(self) -> None:
        """Perform one shared catalog integration shutdown."""
        self._started = False
        errors: list[Exception] = []
        cancelled: asyncio.CancelledError | None = None
        drain_task = asyncio.create_task(self._cleanup_tracker.drain())
        while True:
            try:
                errors.extend(await asyncio.shield(drain_task))
                break
            except asyncio.CancelledError as exc:
                cancelled = cancelled or exc
                if drain_task.done():
                    if not drain_task.cancelled():
                        try:
                            errors.extend(drain_task.result())
                        except Exception as drain_error:
                            errors.append(drain_error)
                    break
                continue
            except Exception as exc:
                errors.append(exc)
                break
        try:
            if self._provider_lease.should_close:
                close_task = asyncio.create_task(self._close_provider())
                retries_remaining = 1
                while True:
                    try:
                        await asyncio.shield(close_task)
                        break
                    except asyncio.CancelledError as close_cancelled:
                        cancelled = cancelled or close_cancelled
                        if close_task.done():
                            if retries_remaining <= 0:
                                break
                            retries_remaining -= 1
                            close_task = asyncio.create_task(self._close_provider())
                    except Exception as close_error:
                        errors.append(close_error)
                        break
        except Exception as exc:
            errors.append(exc)
        finally:
            if self._provider_closed:
                try:
                    self._cleanup_private_cache_directory()
                except Exception as exc:
                    errors.append(exc)
        if cancelled is not None:
            if errors:
                cancelled.add_note(str(ExceptionGroup("Additional catalog shutdown failures.", errors)))
            raise cancelled
        if errors:
            raise ExceptionGroup("Catalog integration shutdown failed.", errors)

    async def _close_provider(self) -> None:
        if self._provider_closed:
            return
        task = self._provider_close_task
        if task is None:

            async def close_once() -> None:
                close = (
                    getattr(self.provider, "aclose", None)
                    or getattr(self.provider, "close", None)
                    or getattr(self.provider, "cleanup", None)
                )
                if not callable(close):
                    raise TypeError("Owned catalog providers must define aclose(), close(), or cleanup().")
                result = close()
                if inspect.isawaitable(result):
                    _ = await result
                self._provider_closed = True

            task = asyncio.create_task(close_once())
            self._provider_close_task = task
        try:
            await asyncio.shield(task)
        finally:
            if task.done() and self._provider_close_task is task:
                self._provider_close_task = None

    def _cleanup_private_cache_directory(self) -> None:
        if self._private_cache_directory is not None:
            shutil.rmtree(self._private_cache_directory)
            self._private_cache_directory = None

    def bind_session(self, context: SessionContext, *, execution_references: bool) -> CatalogSessionBinding:
        """Create isolated caller policy and resolver state for one session."""
        authorizer = self._authorizer_factory(context) if self._authorizer_factory is not None else self._authorizer
        assert authorizer is not None
        extensions: tuple[object, ...] = ()
        try:
            catalog = AuthorizedCatalogProvider(
                self.provider,
                authorizer,
                mode=self._policy_mode,
                per_artifact_enforcer=self._per_artifact_enforcer,
            )
            request_context = _request_context(context)
            resolver = CatalogSessionResolver(catalog, request_context)
            if self._capability_extension_factory is not None:
                created = self._capability_extension_factory(context, catalog, request_context)
                if created is not None:
                    if isinstance(created, (tuple, list)):
                        extensions = tuple(created)
                    else:
                        extensions = (created,)
            binding = CatalogSessionBinding(
                catalog,
                request_context,
                resolver,
                execution_references,
                extensions,
                self._cleanup_tracker,
                authorizer_factory=self._authorizer_factory,
                owned_authorizer=authorizer if self._authorizer_factory is not None else None,
                capability_extension_factory=self._capability_extension_factory,
                provider=self.provider,
                policy_mode=self._policy_mode,
                per_artifact_enforcer=self._per_artifact_enforcer,
            )
            resolver._snapshot = binding.snapshot
            return binding
        except BaseException as bind_error:
            resources = (*extensions, authorizer) if self._authorizer_factory is not None else extensions
            if resources:
                pending_resources = list(resources)

                def cleanup() -> Any:
                    return _close_resources(pending_resources)

                try:
                    self._cleanup_tracker.schedule(cleanup(), retry=cleanup)
                except BaseException as cleanup_error:
                    bind_error.add_note(f"Catalog session binding rollback also failed: {cleanup_error!r}")
            raise

    async def capabilities(
        self,
        binding: CatalogSessionBinding | CatalogSessionView,
        read_capabilities: tuple[SourceCapabilities, ...] | None = None,
    ) -> tuple[Any, ...]:
        by_source = {
            capability.source_id: set(capability.supported_operations)
            for capability in (
                read_capabilities
                if read_capabilities is not None
                else await binding.catalog.capabilities(binding.context)
            )
        }
        for extension in binding.capability_extensions:
            capabilities = getattr(extension, "capabilities", None)
            if not callable(capabilities):
                continue
            extension_capabilities = capabilities(binding.context)
            if inspect.isawaitable(extension_capabilities):
                extension_capabilities = await extension_capabilities
            for capability in extension_capabilities:
                by_source.setdefault(capability.source_id, set()).update(capability.supported_operations)
        return tuple(
            SourceCapabilities(source_id, frozenset(operations)) for source_id, operations in sorted(by_source.items())
        )


def _request_context(context: SessionContext) -> RequestContext:
    return RequestContext(
        request_id=context.session_id,
        caller_id=context.user_identity,
        attributes={
            "session_id": context.session_id,
            "session_type": context.session_type,
            "metadata": dict(context.metadata),
            "claims": dict(context.token_claims),
        },
    )


def _error_payload(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, DataLakeError):
        payload: dict[str, Any] = {"error": _sanitize_error_message(exc.message), "error_type": exc.code.value}
        if exc.operation is not None:
            payload["operation"] = exc.operation
        if exc.resource_id is not None:
            payload["resource_id"] = safe_artifact_reference(exc.resource_id)
        return payload
    if isinstance(exc, ValueError):
        return {"error": _sanitize_error_message(str(exc)), "error_type": "invalid_request"}
    LOGGER.error("Catalog tool failed (%s)", type(exc).__name__)
    return {"error": "Catalog operation failed.", "error_type": "internal"}


def _sanitize_error_message(message: str) -> str:
    return safe_artifact_reference(message)


def _sanitize_metadata_value(value: Any) -> Any:
    if isinstance(value, str):
        return _sanitize_error_message(value)
    if isinstance(value, Mapping):
        return {
            _sanitize_error_message(key) if isinstance(key, str) else key: _sanitize_metadata_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_metadata_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_metadata_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return type(value)(_sanitize_metadata_value(item) for item in value)
    return value


def _agent_safe_identifier(value: str, field_name: str) -> str:
    """Reject locator-shaped identifiers that cannot be safely round-tripped."""
    if contains_artifact_locator(value) or safe_artifact_reference(value) != value:
        raise ValueError(f"Catalog {field_name} must be a logical identifier, not a URI.")
    return value


def _artifact_payload(artifact: Any, *, load_path: str | None = None) -> dict[str, Any]:
    payload = {
        **{
            _sanitize_error_message(key) if isinstance(key, str) else key: _sanitize_metadata_value(value)
            for key, value in artifact.metadata.items()
            if key not in _RESERVED_PAYLOAD_FIELDS
        },
        "id": _agent_safe_identifier(artifact.reference.artifact_id, "artifact ID"),
        "source_id": _agent_safe_identifier(artifact.reference.source_id, "source ID"),
        "name": _sanitize_metadata_value(artifact.presentation.name),
        "current_revision": artifact.revision,
        "content_revision": artifact.content_revision,
        "metadata_revision": artifact.metadata_revision,
        "checksum_sha256": artifact.checksum_sha256,
        "score": artifact.score,
    }
    for key, value in {
        "description": _sanitize_metadata_value(artifact.presentation.description),
        "content_type": _sanitize_metadata_value(artifact.presentation.media_type),
        "size_bytes": artifact.presentation.size_bytes,
    }.items():
        if value is not None:
            payload[key] = value
    payload = {key: value for key, value in payload.items() if value is not None}
    if load_path is not None:
        payload["load_path"] = load_path
    return payload


def register_catalog_discovery_tools(server: Any, integration: CatalogIntegration) -> None:
    """Register policy-aware discovery tools with legacy-compatible payloads."""
    registration_state = getattr(server.mcp, "__dict__", {}).get("_agora_catalog_tool_mode")
    if registration_state is not None:
        raise RuntimeError(
            f"Catalog tools are already registered in {registration_state!r} mode. "
            "Do not combine CatalogIntegration discovery tools with legacy register_catalog_tools(). "
            "The integration intentionally omits query_catalog; mount register_catalog_admin_tools() "
            "on a separately authorized administrative MCP surface when raw SQL compatibility is required."
        )
    setattr(server.mcp, "_agora_catalog_tool_mode", "policy-aware")

    @asynccontextmanager
    async def binding(tool_name: str, mcp_ctx: Context | None) -> AsyncIterator[CatalogSessionView]:
        try:
            session_id = mcp_ctx.session_id if mcp_ctx is not None else None
        except (AttributeError, RuntimeError):
            session_id = None
        restore_auth = getattr(server, "_restore_auth_context_for_mcp_session", None)
        if callable(restore_auth):
            restore_auth(session_id)
        try:
            session = await server._get_or_create_session(tool_name, session_id=session_id)
            session_manager = getattr(server, "session_manager", None)
            resource_operation = (
                session_manager.session_resource_operation(session.session_id)
                if session_manager is not None
                else nullcontext()
            )
            async with resource_operation:
                catalog_binding = session.extensions.get("catalog")
                if catalog_binding is None:
                    raise RuntimeError("Catalog session binding is unavailable.")
                current = snapshot(catalog_binding)
                try:
                    yield current
                finally:
                    current.close()
        finally:
            clear_auth = getattr(server, "_clear_auth_context", None)
            if callable(clear_auth):
                clear_auth()

    def snapshot(current: Any) -> CatalogSessionView:
        take_snapshot = getattr(current, "snapshot", None)
        if callable(take_snapshot):
            return cast(CatalogSessionView, take_snapshot())
        return CatalogSessionView(
            current.catalog,
            current.context,
            current.execution_references,
            tuple(getattr(current, "capability_extensions", ())),
        )

    def execution_reference(
        artifact: Any,
        current: CatalogSessionBinding | CatalogSessionView,
        capabilities: dict[str, SourceCapabilities],
    ) -> str | None:
        if (
            not current.execution_references
            or integration._policy_mode is CatalogPolicyMode.PER_ARTIFACT
            or artifact.locator is None
        ):
            return None
        source = capabilities.get(artifact.reference.source_id)
        if source is None or not source.supports(CatalogOperation.RESOLVE):
            return None
        reference = artifact.reference
        if reference.revision is None and artifact.revision is not None:
            reference = ArtifactReference(reference.artifact_id, reference.source_id, artifact.revision)
        return f"<blob>{_encode_reference(reference)}</blob>"

    async def search_data(
        query: Annotated[str, Field(max_length=2_000)],
        domain: Annotated[str | None, Field(max_length=200)] = None,
        source_type: Annotated[str | None, Field(max_length=50)] = None,
        top: Annotated[int, Field(ge=1, le=_MAX_TOOL_PAGE_SIZE)] = 10,
        cursor: Annotated[str | None, Field(max_length=8_192)] = None,
        mcp_ctx: Context | None = None,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        try:
            async with binding("search_data", mcp_ctx) as current:
                capabilities = {
                    capability.source_id: capability
                    for capability in await current.catalog.capabilities(current.context)
                }
                search_source_ids = tuple(
                    source_id
                    for source_id, capability in capabilities.items()
                    if capability.supports(CatalogOperation.SEARCH)
                )
                if not search_source_ids:
                    return []
                page = await current.catalog.search(
                    SearchRequest(
                        query=query,
                        source_ids=search_source_ids,
                        page=PageRequest(limit=top, cursor=cursor),
                        filters={
                            key: value for key, value in {"domain": domain, "source_type": source_type}.items() if value
                        },
                    ),
                    current.context,
                )
                hits = []
                for artifact in page.items:
                    hits.append(
                        _artifact_payload(
                            artifact,
                            load_path=execution_reference(artifact, current, capabilities),
                        )
                    )
                if page.next_cursor is not None:
                    for hit in hits:
                        hit["next_cursor"] = page.next_cursor
                return hits
        except Exception as exc:
            return _error_payload(exc)

    async def get_artifact(
        artifact_id: Annotated[str, Field(min_length=1, max_length=2_000)],
        source_id: Annotated[str | None, Field(max_length=200)] = None,
        revision: Annotated[int | None, Field(ge=1)] = None,
        mcp_ctx: Context | None = None,
    ) -> dict[str, Any]:
        try:
            async with binding("get_artifact", mcp_ctx) as current:
                capabilities = {
                    capability.source_id: capability
                    for capability in await current.catalog.capabilities(current.context)
                }
                if source_id is None:
                    source_ids = [
                        capability.source_id
                        for capability in capabilities.values()
                        if capability.supports(CatalogOperation.GET)
                    ]
                    matches = []
                    for candidate_source_id in source_ids:
                        try:
                            match = await current.catalog.get(
                                ArtifactReference(artifact_id, candidate_source_id, revision),
                                current.context,
                            )
                        except (ArtifactNotFoundError, PermissionDeniedError):
                            continue
                        matches.append(match)
                    if not matches:
                        raise ArtifactNotFoundError("Artifact not found.", operation="get")
                    if len(matches) > 1:
                        raise ValueError("source_id is required because this artifact ID matches multiple sources.")
                    artifact = matches[0]
                else:
                    artifact = await current.catalog.get(
                        ArtifactReference(artifact_id, source_id, revision), current.context
                    )
                return _artifact_payload(
                    artifact,
                    load_path=execution_reference(artifact, current, capabilities),
                )
        except Exception as exc:
            return _error_payload(exc)

    async def list_domains(mcp_ctx: Context | None = None) -> list[str] | dict[str, Any]:
        try:
            async with binding("list_domains", mcp_ctx) as current:
                list_source_ids = tuple(
                    capability.source_id
                    for capability in await current.catalog.capabilities(current.context)
                    if capability.supports(CatalogOperation.LIST)
                )
                if not list_source_ids:
                    return []
                domains: set[str] = set()
                cursor: str | None = None
                remaining = _MAX_DOMAIN_SCAN
                while remaining:
                    page_size = min(_MAX_TOOL_PAGE_SIZE, remaining)
                    page = await current.catalog.list(
                        ListRequest(
                            source_ids=list_source_ids,
                            page=PageRequest(limit=page_size, cursor=cursor),
                        ),
                        current.context,
                    )
                    domains.update(
                        _sanitize_metadata_value(str(item.metadata["domain"]))
                        for item in page.items
                        if item.metadata.get("domain")
                    )
                    remaining -= len(page.items)
                    cursor = page.next_cursor
                    if cursor is None or not page.items:
                        break
                return sorted(domains)
        except Exception as exc:
            return _error_payload(exc)

    async def get_catalog_capabilities(mcp_ctx: Context | None = None) -> dict[str, Any]:
        try:
            async with binding("get_catalog_capabilities", mcp_ctx) as current:
                read_capabilities = await current.catalog.capabilities(current.context)
                capabilities = await integration.capabilities(current, read_capabilities)
                return {
                    "sources": [
                        {
                            "source_id": _agent_safe_identifier(capability.source_id, "source ID"),
                            "operations": sorted(operation.value for operation in capability.supported_operations),
                        }
                        for capability in capabilities
                    ],
                    "execution_references": (
                        current.execution_references
                        and integration._policy_mode is not CatalogPolicyMode.PER_ARTIFACT
                        and any(capability.supports(CatalogOperation.RESOLVE) for capability in read_capabilities)
                    ),
                }
        except Exception as exc:
            return _error_payload(exc)

    server.mcp.tool(
        name="search_data",
        description=(
            "Search the caller-authorized data catalog. Results retain the existing id/source/name metadata shape "
            "and include load_path only when this session can resolve the artifact in execute_*_code."
        ),
    )(search_data)
    server.mcp.tool(
        name="get_artifact",
        description=(
            "Get caller-authorized artifact metadata by source_id and id. A returned load_path can be pasted "
            "directly into execute_*_code."
        ),
    )(get_artifact)
    server.mcp.tool(
        name="list_domains",
        description="List caller-authorized catalog domains (bounded to the first 1000 visible artifacts).",
    )(list_domains)
    server.mcp.tool(
        name="get_catalog_capabilities",
        description="Return effective caller-authorized catalog operations and execution-reference availability.",
    )(get_catalog_capabilities)


__all__ = [
    "CatalogIntegration",
    "CatalogSessionBinding",
    "CatalogSessionResolver",
    "SessionCredential",
    "register_catalog_discovery_tools",
]
