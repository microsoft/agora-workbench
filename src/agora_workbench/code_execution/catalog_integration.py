"""Opt-in catalog lifecycle and discovery integration for code execution."""

from __future__ import annotations

import base64
import binascii
import asyncio
import inspect
import json
import logging
import re
import shutil
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Annotated

from fastmcp import Context
from pydantic import Field

from agora_workbench.data_lake import (
    ArtifactNotFoundError,
    ArtifactReference,
    CatalogAuthorizer,
    CatalogOperation,
    CatalogPolicyEnforcer,
    CatalogPolicyMode,
    CatalogProvider,
    DataLakeError,
    DevelopmentAllowAllCatalogAuthorizer,
    ListRequest,
    PageRequest,
    RequestContext,
    ResourceLease,
    ResourceOwnership,
    SearchRequest,
    SourceCapabilities,
    sanitize_uri_for_display,
    stable_source_id,
)
from agora_workbench.data_lake.catalog import CatalogConfig, CatalogDB, CatalogIndexer, SourceConfig
from agora_workbench.data_lake.policy import AuthorizedCatalogProvider
from agora_workbench.data_lake.providers import SQLiteCatalogProvider

from .sessions.session import SessionContext

LOGGER = logging.getLogger(__name__)

_MAX_TOOL_PAGE_SIZE = 100
_MAX_DOMAIN_SCAN = 1_000
_REFERENCE_PREFIX = "catalog-v1:"
_URI_IN_TEXT_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'<>]+")
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
        self._tasks: dict[asyncio.Task[None], Callable[[], Any] | None] = {}

    def schedule(self, awaitable: Any, *, retry: Callable[[], Any] | None = None) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(awaitable)
            finally:
                loop.close()
            return
        self._tasks[loop.create_task(awaitable)] = retry

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
                retry = self._tasks.pop(task, None)
                if isinstance(result, asyncio.CancelledError):
                    cancelled = cancelled or result
                    if retry is not None:
                        self.schedule(retry())
                elif isinstance(result, Exception):
                    errors.append(result)
        if cancelled is not None:
            if errors:
                cancelled.add_note(str(ExceptionGroup("Additional catalog cleanup failures.", errors)))
            raise cancelled
        return errors


class SessionCredential:
    """Adapt a workbench credential provider to the async Azure credential shape."""

    def __init__(self, provider: Any):
        self._provider = provider

    async def get_token(self, *scopes: str, **kwargs: object) -> Any:
        del kwargs
        if not scopes:
            raise ValueError("At least one scope is required.")
        return await self._provider.get_token(scopes[0])

    async def close(self) -> None:
        await self._provider.close()

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


class _ConfiguredCatalogProvider(SQLiteCatalogProvider):
    """Lifecycle wrapper for scan, manifest, or mixed configured catalogs."""

    def __init__(self, config: CatalogConfig, *, db_path: str | Path = ":memory:", credential_provider: Any = None):
        self._db_owned = CatalogDB(db_path, vec_dimensions=config.search.embedding_dimensions)
        self._closed = False
        self._embedding_closed = False
        self._db_closed = False
        try:
            self._db_owned.open()
            self._indexer = CatalogIndexer(config, self._db_owned, credential_provider=credential_provider)
            self._configured_source_ids = tuple(_effective_source_id(source) for source in config.sources)
            super().__init__(self._db_owned, self._configured_source_ids)
        except BaseException:
            self._db_owned.close()
            self._closed = True
            raise

    async def load(self) -> int:
        if self._closed:
            raise RuntimeError("Catalog provider is closed.")
        indexed = await self._indexer.index()
        unavailable = []
        for source_id in self._configured_source_ids:
            state = self._db_owned.get_source_refresh_state(source_id)
            if state is None or state.successful_generation < 1:
                unavailable.append(source_id)
        if unavailable:
            raise RuntimeError(f"Catalog sources are not ready: {', '.join(sorted(unavailable))}")
        return indexed

    async def aclose(self) -> None:
        if self._closed:
            return
        errors: list[Exception] = []
        cancelled: asyncio.CancelledError | None = None
        if not self._embedding_closed:
            try:
                embedding_provider = vars(self._indexer).get("_embedding_provider")
                close = getattr(embedding_provider, "aclose", None) or getattr(embedding_provider, "close", None)
                if callable(close):
                    result = close()
                    if inspect.isawaitable(result):
                        await result
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
        self._closed = self._embedding_closed and self._db_closed
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
        artifact_id = payload["artifact_id"]
        source_id = payload["source_id"]
        revision = payload.get("revision")
        if not isinstance(artifact_id, str) or not artifact_id:
            raise ValueError
        if not isinstance(source_id, str) or not source_id:
            raise ValueError
        if revision is not None and (not isinstance(revision, int) or isinstance(revision, bool)):
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
        self._closed = False

    @property
    def unavailable_reason(self) -> str | None:
        return "The catalog resolver has been closed." if self._closed else None

    async def resolve(self, artifact_id: str) -> str:
        if self._closed:
            raise ValueError(self.unavailable_reason)
        resolved = await self._catalog.resolve(_decode_reference(artifact_id), self._context)
        return resolved.locator.uri

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
    _closed: bool = False

    def refresh_context(self, context: SessionContext) -> None:
        """Refresh authorization inputs when a transport session receives a new token."""
        request_context = _request_context(context)
        self.context = request_context
        self.resolver._context = request_context

    async def aclose(self) -> None:
        """Close session-owned extension resources, never the shared read provider."""
        if self._closed:
            return
        self._closed = True
        errors: list[Exception] = []
        cancelled: asyncio.CancelledError | None = None
        try:
            await self.resolver.aclose()
        except asyncio.CancelledError as exc:
            cancelled = exc
        except Exception as exc:
            errors.append(exc)
        for extension in self.capability_extensions:
            close = (
                getattr(extension, "aclose", None)
                or getattr(extension, "close", None)
                or getattr(extension, "cleanup", None)
            )
            if callable(close):
                try:
                    result = close()
                    if inspect.isawaitable(result):
                        await result
                except asyncio.CancelledError as exc:
                    cancelled = cancelled or exc
                except Exception as exc:
                    errors.append(exc)
        if cancelled is not None:
            self._closed = False
            if errors:
                cancelled.add_note(str(ExceptionGroup("Additional catalog session cleanup failures.", errors)))
            raise cancelled
        if errors:
            self._closed = False
            raise ExceptionGroup("Catalog session binding cleanup failed.", errors)

    def cleanup(self) -> None:
        """Schedule complete async cleanup from synchronous lifecycle paths."""
        if self._closed:
            return
        if self.cleanup_tracker is None:
            raise RuntimeError("Catalog session binding has no cleanup tracker.")
        self.cleanup_tracker.schedule(self.aclose(), retry=self.aclose)


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
                    await result
            await self.provider.capabilities()
            self._started = True
        except BaseException as startup_error:
            close_error: BaseException | None = None
            try:
                if self._provider_lease.should_close:
                    await asyncio.shield(self._close_provider())
            except BaseException as exc:
                close_error = exc
            finally:
                self._cleanup_private_cache_directory()
            if close_error is not None:
                startup_error.add_note(f"Catalog startup rollback also failed: {close_error!r}")
            raise

    async def shutdown(self) -> None:
        """Close only resources owned by this integration."""
        self._started = False
        errors: list[Exception] = []
        cancelled: asyncio.CancelledError | None = None
        try:
            errors.extend(await self._cleanup_tracker.drain())
        except asyncio.CancelledError as exc:
            cancelled = exc
        except Exception as exc:
            errors.append(exc)
        try:
            if self._provider_lease.should_close:
                close_task = asyncio.create_task(self._close_provider())
                try:
                    await asyncio.shield(close_task)
                except asyncio.CancelledError as exc:
                    cancelled = cancelled or exc
                    if close_task.cancelled():
                        close_task = asyncio.create_task(self._close_provider())
                    try:
                        await asyncio.shield(close_task)
                    except asyncio.CancelledError as close_cancelled:
                        cancelled = cancelled or close_cancelled
                    except Exception as close_error:
                        errors.append(close_error)
        except Exception as exc:
            errors.append(exc)
        finally:
            self._cleanup_private_cache_directory()
        if cancelled is not None:
            if errors:
                cancelled.add_note(str(ExceptionGroup("Additional catalog shutdown failures.", errors)))
            raise cancelled
        if errors:
            raise ExceptionGroup("Catalog integration shutdown failed.", errors)

    async def _close_provider(self) -> None:
        close = getattr(self.provider, "aclose", None) or getattr(self.provider, "close", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result

    def _cleanup_private_cache_directory(self) -> None:
        if self._private_cache_directory is not None:
            shutil.rmtree(self._private_cache_directory, ignore_errors=True)
            self._private_cache_directory = None

    def bind_session(self, context: SessionContext, *, execution_references: bool) -> CatalogSessionBinding:
        """Create isolated caller policy and resolver state for one session."""
        authorizer = self._authorizer_factory(context) if self._authorizer_factory is not None else self._authorizer
        assert authorizer is not None
        catalog = AuthorizedCatalogProvider(
            self.provider,
            authorizer,
            mode=self._policy_mode,
            per_artifact_enforcer=self._per_artifact_enforcer,
        )
        request_context = _request_context(context)
        resolver = CatalogSessionResolver(catalog, request_context)
        extensions: tuple[object, ...] = ()
        if self._capability_extension_factory is not None:
            created = self._capability_extension_factory(context, catalog, request_context)
            if created is not None:
                if isinstance(created, (tuple, list)):
                    extensions = tuple(created)
                else:
                    extensions = (created,)
        return CatalogSessionBinding(
            catalog,
            request_context,
            resolver,
            execution_references,
            extensions,
            self._cleanup_tracker,
        )

    async def capabilities(self, binding: CatalogSessionBinding) -> tuple[Any, ...]:
        by_source = {
            capability.source_id: set(capability.supported_operations)
            for capability in await binding.catalog.capabilities(binding.context)
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
            payload["resource_id"] = (
                sanitize_uri_for_display(exc.resource_id) if "://" in exc.resource_id else exc.resource_id
            )
        return payload
    if isinstance(exc, ValueError):
        return {"error": _sanitize_error_message(str(exc)), "error_type": "invalid_request"}
    LOGGER.error("Catalog tool failed (%s)", type(exc).__name__)
    return {"error": "Catalog operation failed.", "error_type": "internal"}


def _sanitize_error_message(message: str) -> str:
    return _URI_IN_TEXT_RE.sub(lambda match: sanitize_uri_for_display(match.group(0)), message)


def _sanitize_metadata_value(value: Any) -> Any:
    if isinstance(value, str):
        return _sanitize_error_message(value)
    if isinstance(value, Mapping):
        return {key: _sanitize_metadata_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_metadata_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_metadata_value(item) for item in value)
    return value


def _artifact_payload(artifact: Any, *, load_path: str | None = None) -> dict[str, Any]:
    payload = {
        **{
            key: _sanitize_metadata_value(value)
            for key, value in artifact.metadata.items()
            if key not in _RESERVED_PAYLOAD_FIELDS
        },
        "id": artifact.reference.artifact_id,
        "source_id": artifact.reference.source_id,
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

    async def binding(tool_name: str, mcp_ctx: Context | None) -> CatalogSessionBinding:
        try:
            session_id = mcp_ctx.session_id if mcp_ctx is not None else None
        except (AttributeError, RuntimeError):
            session_id = None
        restore_auth = getattr(server, "_restore_auth_context_for_mcp_session", None)
        if callable(restore_auth):
            restore_auth(session_id)
        session = await server._get_or_create_session(tool_name, session_id=session_id)
        catalog_binding = session.extensions.get("catalog")
        if catalog_binding is None:
            raise RuntimeError("Catalog session binding is unavailable.")
        return catalog_binding

    def clear_auth_context() -> None:
        clear_auth = getattr(server, "_clear_auth_context", None)
        if callable(clear_auth):
            clear_auth()

    def execution_reference(
        artifact: Any,
        current: CatalogSessionBinding,
        capabilities: dict[str, SourceCapabilities],
    ) -> str | None:
        if not current.execution_references:
            return None
        if integration._policy_mode is CatalogPolicyMode.PER_ARTIFACT:
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
            current = await binding("search_data", mcp_ctx)
            page = await current.catalog.search(
                SearchRequest(
                    query=query,
                    page=PageRequest(limit=top, cursor=cursor),
                    filters={
                        key: value for key, value in {"domain": domain, "source_type": source_type}.items() if value
                    },
                ),
                current.context,
            )
            capabilities = {
                capability.source_id: capability for capability in await current.catalog.capabilities(current.context)
            }
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
        finally:
            clear_auth_context()

    async def get_artifact(
        artifact_id: Annotated[str, Field(min_length=1, max_length=2_000)],
        source_id: Annotated[str | None, Field(max_length=200)] = None,
        revision: Annotated[int | None, Field(ge=1)] = None,
        mcp_ctx: Context | None = None,
    ) -> dict[str, Any]:
        try:
            current = await binding("get_artifact", mcp_ctx)
            capabilities = {
                capability.source_id: capability for capability in await current.catalog.capabilities(current.context)
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
                    except ArtifactNotFoundError:
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
        finally:
            clear_auth_context()

    async def list_domains(mcp_ctx: Context | None = None) -> list[str] | dict[str, Any]:
        try:
            current = await binding("list_domains", mcp_ctx)
            domains: set[str] = set()
            cursor: str | None = None
            remaining = _MAX_DOMAIN_SCAN
            while remaining:
                page_size = min(_MAX_TOOL_PAGE_SIZE, remaining)
                page = await current.catalog.list(
                    ListRequest(page=PageRequest(limit=page_size, cursor=cursor)),
                    current.context,
                )
                domains.update(str(item.metadata["domain"]) for item in page.items if item.metadata.get("domain"))
                remaining -= len(page.items)
                cursor = page.next_cursor
                if cursor is None or not page.items:
                    break
            return sorted(domains)
        except Exception as exc:
            return _error_payload(exc)
        finally:
            clear_auth_context()

    async def get_catalog_capabilities(mcp_ctx: Context | None = None) -> dict[str, Any]:
        try:
            current = await binding("get_catalog_capabilities", mcp_ctx)
            read_capabilities = await current.catalog.capabilities(current.context)
            capabilities = await integration.capabilities(current)
            return {
                "sources": [
                    {
                        "source_id": capability.source_id,
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
        finally:
            clear_auth_context()

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
