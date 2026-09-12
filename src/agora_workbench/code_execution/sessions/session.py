"""Generic session container with common functionality."""

import asyncio
import inspect
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, TypeVar, Generic, TYPE_CHECKING

from .objects import ObjectStore

if TYPE_CHECKING:
    from ..data_access.manager import DataLakeDataManager

T = TypeVar("T")


@dataclass(frozen=True)
class SessionResources:
    """A data manager plus server-owned per-session extension state."""

    data_manager: "DataLakeDataManager"
    extensions: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SessionContext:
    """Per-session context handed to a ``data_manager_factory``.

    Carries the identifying fields of the session being created, before the
    :class:`Session` itself exists. This exists so a factory can derive
    per-user or per-session configuration (most commonly from
    ``user_identity`` / ``user_token``) without the circular dependency that
    passing the not-yet-constructed ``Session`` would require.

    Attributes:
        session_id: The ID assigned to the session being created.
        user_identity: Owner's composite identifier from JWT token (``oid@tid``).
        user_token: User's bearer token for authentication.
        token_claims: Cached JWT claims for ``user_token``.
        session_type: Categorizes session type (e.g., "python", "database").
        metadata: Optional key-value metadata supplied at creation.
    """

    session_id: str
    user_identity: str
    user_token: str = field(repr=False)
    token_claims: Dict = field(default_factory=dict, repr=False)
    session_type: str = "default"
    metadata: Dict = field(default_factory=dict)


class Session(Generic[T]):
    """
    Generic session container managing stateful data across MCP server interactions.

    Sessions provide lifecycle management, ownership tracking, and metadata storage for
    persistent resources like code execution environments, database connections, or
    computation state. Each session is owned by a specific user (identified via JWT token
    claims) and tracks access patterns, status transitions, and cleanup requirements.

    Attributes:
        session_id (str): Unique identifier for the session.
        data (T): The session's payload data, type-parameterized for type safety.
        session_type (str): Categorizes session type (e.g., "python", "database").
        user_identity (str): Owner's composite identifier from JWT token (``oid@tid``).
        user_token (str): User's bearer token for authentication.
        metadata (Dict): Optional key-value metadata for session configuration.
        token_claims (Dict): Optional cached JWT token claims for session authorization.
            These claims are used to restore authentication context without re-validating
            the JWT token. Intentionally excluded from get_info() for security.
        created_at (datetime): Timestamp when session was created.
        last_accessed (datetime): Timestamp of most recent session access.
        status (str): Current session state (e.g., "created", "active", "error").
        data_manager (DataLakeDataManager): Manager for DataLake asset access. Owned by
            the session — :meth:`cleanup` tears it down, so an injected manager must not
            be shared between sessions.

    Example:
        >>> from .session import Session
        >>> session = Session(
        ...     session_id="sess_123",
        ...     data={"counter": 0},
        ...     session_type="demo",
        ...     user_identity="user-oid-xyz",
        ...     user_token="eyJ...",
        ...     metadata={"version": "1.0"},
        ...     token_claims={"oid": "user-oid-xyz", "exp": 1234567890},
        ... )
        >>> session.touch()  # Update last accessed time
        >>> session.update_status("active")
        >>> info = session.get_info()
    """

    def __init__(
        self,
        session_id: str,
        data: T,
        session_type: str,
        user_identity: str,
        user_token: str,
        token_claims: Dict,
        metadata: Optional[Dict] = None,
        data_manager: Optional["DataLakeDataManager"] = None,
        extensions: Optional[Dict[str, Any]] = None,
    ):
        """
        Initialize a session.

        Args:
            session_id: Unique identifier for the session.
            data: The session's payload data.
            session_type: Categorizes session type (e.g. ``"python"``).
            user_identity: Owner's composite identifier from JWT token (``oid@tid``).
            user_token: User's bearer token for authentication.
            token_claims: Cached JWT claims for the user token.
            metadata: Optional key-value metadata for session configuration.
            data_manager: Optional pre-built data manager for DataLake asset
                access. When omitted, a default :class:`DataLakeDataManager` is
                constructed. The session takes ownership of whichever manager it
                ends up with — :meth:`cleanup` calls ``cleanup()`` on it — so a
                caller supplying one must pass a fresh instance per session
                rather than a shared singleton.
        """
        self.session_id = session_id
        self.data = data
        self.created_at = datetime.now()
        self.last_accessed = datetime.now()
        self.metadata = metadata or {}
        self.user_identity = user_identity
        self.user_token = user_token
        self.token_claims = token_claims
        self.status = "created"
        self.session_type = session_type
        self.extensions = extensions or {}
        self._asset_counter: int = 0
        self._status_history = [("created", datetime.now())]
        self._scheduled_cleanup_tasks: set[asyncio.Task[None]] = set()

        # Initialize data manager for DataLake asset access. Constructing the
        # default lazily matters: DataLakeDataManager.__init__ eagerly allocates
        # a temp cache dir, so building one only to discard it would leak it.
        if data_manager is None:
            from ..data_access.manager import DataLakeDataManager

            data_manager = DataLakeDataManager()

        self.data_manager = data_manager

        # Initialize object store for asset objects
        self.object_store = ObjectStore()

    def touch(self):
        """Update last accessed timestamp."""
        self.last_accessed = datetime.now()

    def update_status(self, new_status: str):
        """Update session status with history tracking."""
        self.status = new_status
        self._status_history.append((new_status, datetime.now()))
        self.touch()

    def get_info(self) -> Dict[str, Any]:
        """Return session information."""
        age_seconds = (datetime.now() - self.created_at).total_seconds()
        idle_seconds = (datetime.now() - self.last_accessed).total_seconds()

        return {
            "session_id": self.session_id,
            "session_type": self.session_type,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "last_accessed": self.last_accessed.isoformat(),
            "age_seconds": age_seconds,
            "idle_seconds": idle_seconds,
            "metadata": self.metadata,
            "user_identity": self.user_identity,
            "status_history": [{"status": s, "timestamp": t.isoformat()} for s, t in self._status_history],
        }

    def cleanup(self) -> tuple[asyncio.Task[None], ...]:
        """
        Start cleanup of every session resource, including async-only clients.

        Async cleanup tasks are retained until the SessionManager claims them;
        callers that need completion should use :meth:`aclose`.
        """
        errors: list[Exception] = []
        cancellations: list[asyncio.CancelledError] = []
        self._cleanup_resource_sync(self.data_manager, "data manager", errors, cancellations)
        for resource in self.extensions.values():
            self._cleanup_resource_sync(resource, f"extension {type(resource).__name__}", errors, cancellations)
        self._cleanup_resource_sync(self.data, "session payload", errors, cancellations)
        try:
            self._cleanup_session_file()
        except Exception as exc:
            errors.append(exc)
        tasks = tuple(self._scheduled_cleanup_tasks)
        if cancellations:
            if errors:
                cancellations[0].add_note(str(ExceptionGroup("Additional session cleanup failures.", errors)))
            raise cancellations[0]
        if errors:
            raise ExceptionGroup("Session cleanup failed.", errors)
        return tasks

    async def aclose(self) -> None:
        """Attempt all asynchronous cleanup steps, then report aggregated failures."""
        errors: list[Exception] = []
        cancellations: list[asyncio.CancelledError] = []
        await self._cleanup_resource_async(self.data_manager, "data manager", errors, cancellations)
        for resource in self.extensions.values():
            await self._cleanup_resource_async(
                resource,
                f"extension {type(resource).__name__}",
                errors,
                cancellations,
            )
        await self._cleanup_resource_async(self.data, "session payload", errors, cancellations)
        try:
            self._cleanup_session_file()
        except Exception as exc:
            errors.append(exc)
        retry_tasks = tuple(self._scheduled_cleanup_tasks)
        if retry_tasks:
            results = await asyncio.gather(
                *(asyncio.shield(task) for task in retry_tasks),
                return_exceptions=True,
            )
            for task, result in zip(retry_tasks, results):
                if task.done():
                    self._scheduled_cleanup_tasks.discard(task)
                if isinstance(result, asyncio.CancelledError):
                    cancellations.append(result)
                elif isinstance(result, Exception):
                    errors.append(result)
        if cancellations:
            if errors:
                cancellations[0].add_note(str(ExceptionGroup("Additional session cleanup failures.", errors)))
            raise cancellations[0]
        if errors:
            raise ExceptionGroup("Session cleanup failed.", errors)

    def take_cleanup_tasks(self) -> tuple[asyncio.Task[None], ...]:
        """Transfer ownership of sync-scheduled cleanup tasks to the manager."""
        tasks = tuple(self._scheduled_cleanup_tasks)
        self._scheduled_cleanup_tasks.clear()
        return tasks

    def _cleanup_resource_sync(
        self,
        resource: object,
        label: str,
        errors: list[Exception],
        cancellations: list[asyncio.CancelledError],
    ) -> None:
        close = (
            getattr(resource, "aclose", None) or getattr(resource, "cleanup", None) or getattr(resource, "close", None)
        )
        if not callable(close):
            return
        try:
            result = close()
            if not inspect.isawaitable(result):
                return
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(result)
                finally:
                    loop.close()
            else:

                async def await_cleanup() -> None:
                    try:
                        await result
                    except asyncio.CancelledError as cancelled:
                        try:
                            await self._retry_resource_cleanup(resource, label)
                        except BaseException as retry_error:
                            cancelled.add_note(f"{label} cleanup retry also failed: {retry_error!r}")
                        raise cancelled

                task = loop.create_task(await_cleanup())
                self._scheduled_cleanup_tasks.add(task)
        except asyncio.CancelledError as exc:
            cancellations.append(exc)
            self._schedule_cleanup_retry(resource, label)
        except Exception as exc:
            errors.append(RuntimeError(f"{label} cleanup failed: {exc}"))

    async def _cleanup_resource_async(
        self,
        resource: object,
        label: str,
        errors: list[Exception],
        cancellations: list[asyncio.CancelledError],
    ) -> None:
        close = (
            getattr(resource, "aclose", None) or getattr(resource, "cleanup", None) or getattr(resource, "close", None)
        )
        if not callable(close):
            return
        try:
            result = close()
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError as exc:
            cancellations.append(exc)
            retry = asyncio.create_task(self._retry_resource_cleanup(resource, label))
            self._scheduled_cleanup_tasks.add(retry)
        except Exception as exc:
            errors.append(RuntimeError(f"{label} cleanup failed: {exc}"))

    def _schedule_cleanup_retry(self, resource: object, label: str) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        retry = loop.create_task(self._retry_resource_cleanup(resource, label))
        self._scheduled_cleanup_tasks.add(retry)

    @staticmethod
    async def _retry_resource_cleanup(resource: object, label: str) -> None:
        close = (
            getattr(resource, "aclose", None) or getattr(resource, "cleanup", None) or getattr(resource, "close", None)
        )
        if not callable(close):
            return
        try:
            result = close()
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise RuntimeError(f"{label} cleanup retry failed: {exc}") from exc

    def _cleanup_session_file(self) -> None:
        """Remove the session file and its owned directory, if present."""
        if not isinstance(self.data, dict) or "session_file" not in self.data:
            return
        session_file = Path(self.data["session_file"])
        if not session_file.exists():
            return
        session_file.unlink()
        session_dir = session_file.parent
        if f"session_{self.session_id}" in str(session_dir):
            try:
                shutil.rmtree(session_dir)
            except OSError:
                pass
