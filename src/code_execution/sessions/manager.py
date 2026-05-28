"""Session manager with automatic cleanup."""

import asyncio
import logging
import mimetypes
import os
import queue
import shutil
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Optional, Tuple, TYPE_CHECKING

from jupyter_client.manager import AsyncKernelManager

from .session import Session
from .storage import InMemoryStorage, SessionStorageBackend

if TYPE_CHECKING:
    from jupyter_client.asynchronous.client import AsyncKernelClient


LOGGER = logging.getLogger(__name__)


_MAX_COMPLETED_JOBS = 200
_COMPLETED_JOB_TTL_SECONDS = 3600.0  # 1 hour

# Artifact pipeline: each session gets a dedicated outputs subdirectory inside
# the container/host.  Files written there during an execute become artifacts:
# the session manager registers them with a UUID token, the MCP server exposes
# a streaming download endpoint keyed on that token, and the activity UI shows
# a download row.  No agent tool call is required — discovery happens by
# diffing the directory snapshot taken before/after each execute.
_OUTPUTS_BASE_DIR = Path(
    os.environ.get("AGORA_OUTPUTS_BASE_DIR", str(Path.home() / "agora-outputs"))
)

# File names ignored when scanning the outputs dir.  Suffix match (`.pyc`,
# `.pyo`) and exact-component match (`__pycache__`, `.ipynb_checkpoints`).
# Hidden files (starting with `.`) are also ignored to avoid surfacing
# editor swap files, lock files, etc.
_ARTIFACT_DENY_SUFFIXES: Tuple[str, ...] = (".pyc", ".pyo")
_ARTIFACT_DENY_COMPONENTS: Tuple[str, ...] = ("__pycache__", ".ipynb_checkpoints")

# Per-artifact size cap.  Activity events carry only metadata, so the cap
# isn't about event size — it's about avoiding registering accidental
# multi-gigabyte artifacts that the user didn't mean to expose.
_MAX_ARTIFACT_SIZE_BYTES: int = 500 * 1024 * 1024  # 500 MB


@dataclass
class _ArtifactRecord:
    """In-memory mapping from download token to a file on disk.

    Lives on ``SessionManager._session_artifacts[session_id][token]``.  The
    HTTP download endpoint looks the file up by (session_id, token) and
    streams it from ``path``; the URL's filename component is purely
    cosmetic and not used for the lookup.
    """

    token: str
    path: Path
    name: str
    size_bytes: int
    mime_type: str
    modified_at: float


class MaxSessionsReachedError(RuntimeError):
    """Raised when creating a session would exceed the configured session limit."""


@dataclass
class _BackgroundJob:
    """Internal state for a background kernel execution."""

    job_id: str
    session_id: str
    msg_id: str
    timeout: float
    start_time: float
    user_identity: str = ""
    status: str = "running"
    stdout_parts: list[str] = field(default_factory=list)
    stderr_parts: list[str] = field(default_factory=list)
    success: bool = True
    error: Optional[str] = None
    completed_at: Optional[float] = None
    task: Optional[asyncio.Task] = None
    # Pre-execute snapshot of the session's outputs dir, captured at
    # submit time; the terminal handler diffs against the post-execute
    # snapshot to surface new/modified files as artifacts.  Mirrors the
    # before/after pattern in :meth:`_execute_code_locked`.
    outputs_before: dict[str, Tuple[int, float]] = field(default_factory=dict)
    artifacts: list[dict] = field(default_factory=list)


class SessionConfig:
    """Configuration for session manager."""

    def __init__(
        self,
        max_sessions: int = 100,
        timeout_minutes: int = 30,
        cleanup_interval_seconds: int = 300,  # 5 minutes
        storage_backend: Optional[SessionStorageBackend] = None,
    ):
        self.max_sessions = max_sessions
        self.timeout = timedelta(minutes=timeout_minutes)
        self.cleanup_interval = timedelta(seconds=cleanup_interval_seconds)
        self.storage_backend = storage_backend or InMemoryStorage()


class SessionManager:
    def _touch_session_if_present(self, session_id: str) -> bool:
        """Atomically refresh a session's last-activity timestamp if it still exists."""
        with self._session_lifecycle_lock:
            session_for_keepalive = self.storage.retrieve(session_id)
            if session_for_keepalive is None:
                return False
            session_for_keepalive.touch()
            self.storage.store(session_id, session_for_keepalive)
            return True

    """Generic session manager with automatic cleanup."""

    def __init__(self, config: Optional[SessionConfig] = None):
        self.config = config or SessionConfig()
        self.storage = self.config.storage_backend
        self._last_cleanup = datetime.now()
        self._session_lifecycle_lock = RLock()
        timeout_seconds = self.config.timeout.total_seconds()
        self.execution_session_keepalive_seconds = max(0.5, min(timeout_seconds / 10.0, 60.0))

        # session_id -> (KernelManager, KernelClient)
        self._kernels: dict[str, Tuple[AsyncKernelManager, "AsyncKernelClient"]] = {}
        self._kernel_last_used: dict[str, float] = {}  # session_id -> timestamp
        self._kernel_tokens: dict[str, Optional[str]] = {}  # session_id -> last injected user token
        # Per-session lock that serializes execute_code_for_session calls so the
        # shared Jupyter kernel client (single iopub queue) cannot be raced by
        # concurrent callers (e.g. four parallel push_object MCP calls).
        self._kernel_execute_locks: dict[str, asyncio.Lock] = {}
        self._background_jobs: dict[str, _BackgroundJob] = {}
        self._session_running_jobs: dict[str, str] = {}
        # Artifact pipeline state.  ``_session_artifacts`` is the
        # token -> record map used by the HTTP download endpoint;
        # ``_kernel_outputs_initialized`` tracks which kernels have already
        # received the AGORA_OUTPUT_DIR preamble so we only inject it once.
        self._session_artifacts: dict[str, dict[str, _ArtifactRecord]] = {}
        self._kernel_outputs_initialized: set[str] = set()

        LOGGER.info(
            f"Initialized SessionManager: max_sessions={self.config.max_sessions}, "
            f"timeout={self.config.timeout.total_seconds() / 60}min"
        )

    def create_session(
        self,
        data: Any,
        user_identity: str,
        user_token: str,
        token_claims: dict,
        metadata: Optional[dict] = None,
        session_id: Optional[str] = None,
    ) -> str:
        """
        Create a new session and return its ID.

        Args:
            data: The data/object to store in the session
            user_identity: User identity (Entra ID, email, etc.)
            user_token: User's bearer token for authentication
            metadata: Optional metadata dict
            session_id: Optional custom session ID (default: UUID)
            token_claims: Cached JWT claims for the user token.
                Stored on the session so that background tasks can restore
                the full auth context without re-validating the token.

        Returns:
            Session ID string
        """
        # Run periodic cleanup
        self._maybe_cleanup()

        with self._session_lifecycle_lock:
            # Enforce max sessions limit
            self._enforce_max_sessions()

            # Generate session ID
            if session_id is None:
                session_id = str(uuid.uuid4())

            # Create session
            session = Session(
                session_id=session_id,
                data=data,
                session_type="default",
                user_identity=user_identity,
                user_token=user_token,
                token_claims=token_claims,
                metadata=metadata,
            )

            # Store
            self.storage.store(session_id, session)

            # Create the per-session outputs directory.  Done eagerly so the
            # kernel can write to it on the very first execute.  Failure to
            # create is non-fatal: artifact discovery just becomes a no-op for
            # this session.
            try:
                self._get_outputs_dir(session_id).mkdir(parents=True, exist_ok=True)
            except OSError:
                LOGGER.warning(
                    "Could not create outputs dir for session %s under %s; "
                    "artifact download will be disabled for this session.",
                    session_id,
                    _OUTPUTS_BASE_DIR,
                    exc_info=True,
                )

        LOGGER.info(f"Created session {session_id} (total={self.storage.count()})")

        return session_id

    def get_session(self, session_id: str) -> Session:
        """
        Get a session by ID, updating its access time.

        Returns:
            Session object

        Raises:
            ValueError: If session not found or expired
        """
        self._maybe_cleanup()

        session = self.storage.retrieve(session_id)

        if session is None:
            raise ValueError(
                f"Session {session_id} not found. It may have expired or been "
                f"cleaned up. Active sessions: {self.storage.count()}"
            )

        # Update access time
        session.touch()
        self.storage.store(session_id, session)

        return session

    def update_session(self, session_id: str, session: Session):
        """Update an existing session."""
        if self.storage.retrieve(session_id) is None:
            raise ValueError(f"Session {session_id} not found")

        session.touch()
        self.storage.store(session_id, session)

    def update_status(self, session_id: str, status: str):
        """Update the status of a session."""
        session = self.get_session(session_id)
        session.update_status(status)
        self.storage.store(session_id, session)

    def close_session(self, session_id: str):
        """
        Explicitly close a session.

        Attempts to clean up resources first. If cleanup fails, the session
        is still deleted to prevent session accumulation, but the error is logged.

        Args:
            session_id: ID of the session to close
        """
        running_job_id = self._get_running_job_for_session(session_id)
        if running_job_id:
            job = self._background_jobs.get(running_job_id)
            if job:
                job.success = False
                job.error = f"Session {session_id} was closed while job {running_job_id} was running"
                job.status = "failed"
                if job.task and not job.task.done():
                    job.task.cancel()

        if session_id in self._kernels:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.create_task(self._shutdown_kernel(session_id))
                else:
                    loop.run_until_complete(self._shutdown_kernel(session_id))
            except Exception as e:
                LOGGER.error(f"Failed to shutdown kernel for session {session_id}: {e}")

        session = self.storage.retrieve(session_id)

        if session:
            # Run cleanup
            cleanup_failed = False
            try:
                session.cleanup()
            except Exception as e:
                cleanup_failed = True
                LOGGER.error(
                    f"Error during cleanup of session {session_id}: {e}. "
                    f"Session will still be removed, but resources may be leaked."
                )

            # Remove from storage
            self.storage.delete(session_id)
            if cleanup_failed:
                LOGGER.warning(f"Closed session {session_id} with failed cleanup (remaining={self.storage.count()})")
            else:
                LOGGER.info(f"Closed session {session_id} (remaining={self.storage.count()})")

    # ========================================================================
    # Jupyter Kernel Management
    # ========================================================================

    async def _get_or_create_kernel(
        self,
        session_id: str,
        working_dir: Optional[str] = None,
        user_token: Optional[str] = None,
        user_identity: Optional[str] = None,
    ) -> Tuple:
        """Get existing kernel for session or create a new one.

        Args:
            session_id: Session identifier.
            working_dir: Optional working directory for the kernel process.
            user_token: User bearer token to expose as ``USER_ASSERTION_TOKEN``
                in the kernel environment. Used by kernel-side tools to authenticate
                with downstream services on behalf of the user.
            user_identity: User identity string to expose as ``USER_IDENTITY``
                in the kernel environment.
        """
        if session_id in self._kernels:
            LOGGER.debug(f"Reusing kernel for session {session_id}")
            self._kernel_last_used[session_id] = time.time()
            return self._kernels[session_id]

        # Start new kernel
        LOGGER.info(f"Starting new Jupyter kernel for session {session_id}")
        kernel_manager = AsyncKernelManager(kernel_name="tools-py")

        # Ensure executables from the selected Python environment are on PATH.
        # The kernelspec argv points at the environment's python, but PATH is
        # inherited from the server process unless explicitly set.
        env = os.environ.copy()
        try:
            python_argv0 = kernel_manager.kernel_spec.argv[0]
            python_path = Path(python_argv0)
            bin_dir = python_path.parent
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"

            # Helpful hints for tooling that inspects these.
            prefix = bin_dir.parent
            if (prefix / "conda-meta").exists():
                env.setdefault("CONDA_PREFIX", str(prefix))
                env.setdefault("CONDA_DEFAULT_ENV", str(prefix.name))
            else:
                env.setdefault("VIRTUAL_ENV", str(prefix))
        except Exception:
            # If anything about kernelspec inspection fails, fall back to
            # inherited env; code execution will still work, but external
            # executables may not be discoverable.
            LOGGER.debug("Kernelspec inspection failed; falling back to inherited PATH", exc_info=True)

        # Explicitly control these variables to avoid inheriting stale values
        # from the server process environment.
        if user_token is not None:
            env["USER_ASSERTION_TOKEN"] = user_token
        else:
            env.pop("USER_ASSERTION_TOKEN", None)

        if user_identity is not None:
            env["USER_IDENTITY"] = user_identity
        else:
            env.pop("USER_IDENTITY", None)

        await kernel_manager.start_kernel(env=env, cwd=working_dir)
        kernel_client = kernel_manager.client()
        kernel_client.start_channels()
        await kernel_client.wait_for_ready()

        # Store in registry
        self._kernels[session_id] = (kernel_manager, kernel_client)
        self._kernel_last_used[session_id] = time.time()
        self._kernel_tokens[session_id] = user_token

        LOGGER.info(f"Kernel started for session {session_id}")
        return kernel_manager, kernel_client

    def _get_running_job_for_session(self, session_id: str) -> Optional[str]:
        """Return a running background job id for the session, if any."""
        job_id = self._session_running_jobs.get(session_id)
        if not job_id:
            return None

        job = self._background_jobs.get(job_id)
        if not job or job.status != "running":
            self._session_running_jobs.pop(session_id, None)
            return None
        return job_id

    def _mark_job_finished(self, job: _BackgroundJob) -> None:
        """Finalize a background job state and clear session busy markers."""
        job.completed_at = time.monotonic()
        if self._session_running_jobs.get(job.session_id) == job.job_id:
            self._session_running_jobs.pop(job.session_id, None)
        self._purge_old_completed_jobs()

    def _purge_old_completed_jobs(self) -> None:
        """Remove completed/failed jobs that exceed the TTL or max-retained count."""
        now = time.monotonic()
        to_delete = [
            jid
            for jid, j in self._background_jobs.items()
            if j.status != "running"
            and j.completed_at is not None
            and (now - j.completed_at) > _COMPLETED_JOB_TTL_SECONDS
        ]
        for jid in to_delete:
            del self._background_jobs[jid]

        finished = sorted(
            ((jid, j) for jid, j in self._background_jobs.items() if j.status != "running"),
            key=lambda x: x[1].completed_at or 0.0,
        )
        excess = len(finished) - _MAX_COMPLETED_JOBS
        if excess > 0:
            for jid, _ in finished[:excess]:
                del self._background_jobs[jid]

    def _prepare_code_with_token_preamble(self, session_id: str, code: str, user_token: Optional[str]) -> str:
        """Inject or clear USER_ASSERTION_TOKEN preamble when session token changes."""
        last_token = self._kernel_tokens.get(session_id)
        if user_token and user_token != last_token:
            token_preamble = (
                "import os as __agora_os__\n"
                f"__agora_os__.environ['USER_ASSERTION_TOKEN'] = {user_token!r}\n"
                "del __agora_os__\n"
            )
            self._kernel_tokens[session_id] = user_token
            return token_preamble + code

        if last_token is not None and not user_token:
            token_preamble = (
                "import os as __agora_os__\n"
                "if 'USER_ASSERTION_TOKEN' in __agora_os__.environ:\n"
                "    del __agora_os__.environ['USER_ASSERTION_TOKEN']\n"
                "del __agora_os__\n"
            )
            self._kernel_tokens.pop(session_id, None)
            return token_preamble + code

        return code

    # ------------------------------------------------------------------
    # Artifact pipeline
    # ------------------------------------------------------------------

    def _get_outputs_dir(self, session_id: str) -> Path:
        """Per-session subdir under :data:`_OUTPUTS_BASE_DIR`."""
        return _OUTPUTS_BASE_DIR / session_id

    def _prepare_outputs_preamble(self, session_id: str) -> str:
        """Inject AGORA_OUTPUT_DIR setup into the kernel once per session.

        Sets both the env var (so subprocess and library code see it) and a
        bare ``AGORA_OUTPUT_DIR`` symbol in the kernel namespace (so the
        agent can ``df.to_csv(f"{AGORA_OUTPUT_DIR}/x.csv")`` without an
        ``import os``).  Subsequent executes return empty; the kernel
        already has these set.
        """
        if session_id in self._kernel_outputs_initialized:
            return ""
        outputs_dir = str(self._get_outputs_dir(session_id))
        self._kernel_outputs_initialized.add(session_id)
        return (
            "import os as __agora_os__\n"
            f"__agora_os__.environ['AGORA_OUTPUT_DIR'] = {outputs_dir!r}\n"
            f"AGORA_OUTPUT_DIR = {outputs_dir!r}\n"
            "del __agora_os__\n"
        )

    def _is_denylisted_artifact(self, path: Path) -> bool:
        """Skip caches, hidden files, and known-noise filenames."""
        if path.name.startswith("."):
            return True
        if path.suffix in _ARTIFACT_DENY_SUFFIXES:
            return True
        if any(part in _ARTIFACT_DENY_COMPONENTS for part in path.parts):
            return True
        return False

    def _snapshot_outputs_dir(self, session_id: str) -> dict[str, Tuple[int, float]]:
        """Return ``{relative_path: (size, mtime)}`` for files in the outputs dir.

        Used to diff before/after each execute and identify new or modified
        artifacts.  Returns empty if the dir doesn't exist (session whose
        mkdir failed at create time, or a path-misconfigured deployment).
        """
        outputs_dir = self._get_outputs_dir(session_id)
        if not outputs_dir.is_dir():
            return {}
        snapshot: dict[str, Tuple[int, float]] = {}
        for path in outputs_dir.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(outputs_dir)
            if self._is_denylisted_artifact(rel):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            snapshot[str(rel)] = (stat.st_size, stat.st_mtime)
        return snapshot

    def _register_artifacts_from_diff(
        self,
        session_id: str,
        before: dict[str, Tuple[int, float]],
        after: dict[str, Tuple[int, float]],
    ) -> list[dict]:
        """Diff two snapshots and register each new/changed file as an artifact.

        Returns a list of metadata dicts suitable for the
        ``CodeExecutionResult.artifacts`` field.  The dicts contain a
        ``download_token`` placeholder rather than a fully-qualified URL —
        URL composition happens in the MCP server layer where the public
        base URL is known.
        """
        outputs_dir = self._get_outputs_dir(session_id)
        registry = self._session_artifacts.setdefault(session_id, {})
        records: list[dict] = []
        for rel_path, after_info in after.items():
            if before.get(rel_path) == after_info:
                continue  # unchanged
            size_bytes, mtime = after_info
            if size_bytes > _MAX_ARTIFACT_SIZE_BYTES:
                LOGGER.warning(
                    "Skipping artifact %s in session %s: %d bytes exceeds cap %d",
                    rel_path,
                    session_id,
                    size_bytes,
                    _MAX_ARTIFACT_SIZE_BYTES,
                )
                continue
            token = uuid.uuid4().hex
            record = _ArtifactRecord(
                token=token,
                path=outputs_dir / rel_path,
                name=rel_path,
                size_bytes=size_bytes,
                mime_type=mimetypes.guess_type(rel_path)[0] or "application/octet-stream",
                modified_at=mtime,
            )
            registry[token] = record
            records.append(
                {
                    "name": rel_path,
                    "size_bytes": size_bytes,
                    "mime_type": record.mime_type,
                    "modified_at": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
                    "download_token": token,
                }
            )
        return records

    def get_artifact_record(self, session_id: str, token: str) -> Optional[_ArtifactRecord]:
        """Look up an artifact for the download endpoint.

        Returns ``None`` for unknown sessions/tokens *and* for tokens whose
        backing file has been removed — both surface as 404 at the HTTP
        layer.
        """
        record = self._session_artifacts.get(session_id, {}).get(token)
        if record is None:
            return None
        if not record.path.is_file():
            return None
        return record

    def _cleanup_session_artifacts(self, session_id: str) -> None:
        """Drop the token map and remove the on-disk outputs dir."""
        self._session_artifacts.pop(session_id, None)
        self._kernel_outputs_initialized.discard(session_id)
        outputs_dir = self._get_outputs_dir(session_id)
        if outputs_dir.exists():
            try:
                shutil.rmtree(outputs_dir, ignore_errors=True)
            except OSError:
                LOGGER.warning("Failed to remove outputs dir for session %s", session_id, exc_info=True)

    def _finalize_background_artifacts(self, job: _BackgroundJob) -> None:
        """Diff the outputs dir against the pre-execute snapshot and record
        new/modified files on the job.

        Best-effort: snapshot or registration failures are logged and leave
        ``job.artifacts`` empty rather than failing the job — matches the
        foreground path's behavior in :meth:`_execute_code_locked`.  Skipped
        when the session has already been cleaned up (the outputs dir is
        gone, so the snapshot would be empty anyway).
        """
        try:
            outputs_after = self._snapshot_outputs_dir(job.session_id)
            job.artifacts = self._register_artifacts_from_diff(
                job.session_id, job.outputs_before, outputs_after
            )
        except Exception:
            LOGGER.warning(
                "Background artifact discovery failed for session %s job %s",
                job.session_id,
                job.job_id,
                exc_info=True,
            )

    async def _collect_background_job(
        self,
        job: _BackgroundJob,
        km: AsyncKernelManager,
        kc: "AsyncKernelClient",
    ) -> None:
        """Collect iopub messages for a running background execution."""
        keepalive_interval_seconds = self.execution_session_keepalive_seconds
        last_keepalive = job.start_time - keepalive_interval_seconds
        try:
            while True:
                now = time.monotonic()
                if now - last_keepalive >= keepalive_interval_seconds:
                    if not self._touch_session_if_present(job.session_id):
                        job.success = False
                        job.error = (
                            f"Session {job.session_id} was cleaned up while execution was in progress. "
                            "Please create a new session and retry."
                        )
                        job.status = "failed"
                        self._mark_job_finished(job)
                        return
                    last_keepalive = now

                if now - job.start_time > job.timeout:
                    await km.interrupt_kernel()
                    await asyncio.sleep(1.0)
                    job.success = False
                    job.error = f"Execution timeout after {job.timeout}s"
                    job.status = "failed"
                    # Capture anything written before the interrupt fired.
                    self._finalize_background_artifacts(job)
                    self._mark_job_finished(job)
                    return

                try:
                    msg = await kc.get_iopub_msg(timeout=1.0)
                except queue.Empty:
                    continue

                msg_type = msg["msg_type"]
                content = msg.get("content", {})
                parent_id = msg.get("parent_header", {}).get("msg_id")

                if parent_id != job.msg_id:
                    continue

                if msg_type == "stream":
                    stream_name = content.get("name", "stdout")
                    text = content.get("text", "")
                    if stream_name == "stdout":
                        job.stdout_parts.append(text)
                    elif stream_name == "stderr":
                        job.stderr_parts.append(text)
                elif msg_type == "error":
                    traceback = "\n".join(content.get("traceback", []))
                    job.stderr_parts.append(traceback)
                    job.success = False
                elif msg_type == "execute_result":
                    data = content.get("data", {})
                    text_result = data.get("text/plain", "")
                    if text_result:
                        job.stdout_parts.append(text_result)
                elif msg_type == "status" and content.get("execution_state") == "idle":
                    while True:
                        try:
                            trailing_msg = await asyncio.wait_for(kc.get_iopub_msg(timeout=0.2), timeout=0.5)
                        except (queue.Empty, asyncio.TimeoutError):
                            break
                        r_type = trailing_msg["msg_type"]
                        r_content = trailing_msg.get("content", {})
                        r_parent = trailing_msg.get("parent_header", {}).get("msg_id")
                        if r_parent != job.msg_id:
                            continue
                        if r_type == "stream":
                            txt = r_content.get("text", "")
                            if r_content.get("name") == "stdout":
                                job.stdout_parts.append(txt)
                            elif r_content.get("name") == "stderr":
                                job.stderr_parts.append(txt)
                        elif r_type == "execute_result":
                            data = r_content.get("data", {})
                            t = data.get("text/plain", "")
                            if t:
                                job.stdout_parts.append(t)
                    break

            job.status = "completed" if job.success else "failed"
            self._kernel_last_used[job.session_id] = time.time()
            self._finalize_background_artifacts(job)
            self._mark_job_finished(job)
        except Exception as e:
            job.success = False
            job.error = f"Background job failed: {e}"
            job.stderr_parts.append(str(e))
            job.status = "failed"
            self._finalize_background_artifacts(job)
            self._mark_job_finished(job)

    async def start_background_execution_for_session(
        self, session_id: str, code: str, timeout: float, working_dir: Optional[str] = None
    ) -> dict[str, str]:
        """Start execution and return immediately with a background job id."""
        try:
            session = self.get_session(session_id)
            user_token = session.user_token
            user_identity = session.user_identity
        except ValueError:
            raise ValueError(
                f"Session {session_id} is no longer available (expired or cleaned up). "
                "Please create a new session and retry."
            ) from None

        running_job_id = self._get_running_job_for_session(session_id)
        if running_job_id:
            raise RuntimeError(f"Session busy — job {running_job_id} is still running")

        km, kc = await self._get_or_create_kernel(
            session_id, working_dir, user_token=user_token, user_identity=user_identity
        )
        # Same preamble shape as the foreground path: outputs preamble
        # must run before the token preamble so AGORA_OUTPUT_DIR is
        # populated even when this background call is the kernel's
        # first execute.  Snapshot before kc.execute so the terminal
        # handler can diff for artifacts.
        code = (
            self._prepare_outputs_preamble(session_id)
            + self._prepare_code_with_token_preamble(session_id, code, user_token)
        )
        outputs_before = self._snapshot_outputs_dir(session_id)
        msg_id = kc.execute(code)

        job_id = f"j_{uuid.uuid4().hex[:12]}"
        job = _BackgroundJob(
            job_id=job_id,
            session_id=session_id,
            msg_id=msg_id,
            timeout=timeout,
            start_time=time.monotonic(),
            user_identity=user_identity,
            outputs_before=outputs_before,
        )
        job.task = asyncio.create_task(self._collect_background_job(job, km, kc))
        self._background_jobs[job_id] = job
        self._session_running_jobs[session_id] = job_id

        return {"job_id": job_id, "status": job.status, "session_id": session_id}

    def check_background_job(self, job_id: str, caller_identity: Optional[str] = None) -> dict[str, Any]:
        """Return current status/output for a background job.

        Args:
            job_id: Background job identifier.
            caller_identity: When provided, the caller's user identity is compared to
                the identity that submitted the job. Both a missing job and an identity
                mismatch raise ``ValueError("Job {job_id} not found")`` so that callers
                cannot use differing error messages to probe whether a job ID exists.
        """
        job = self._background_jobs.get(job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")

        if caller_identity and job.user_identity and caller_identity != job.user_identity:
            raise ValueError(f"Job {job_id} not found")

        now = time.monotonic()
        elapsed_seconds = (job.completed_at or now) - job.start_time
        result: dict[str, Any] = {
            "job_id": job.job_id,
            "session_id": job.session_id,
            "status": job.status,
            "elapsed_seconds": round(elapsed_seconds, 3),
        }

        if job.status == "running":
            result["stdout"] = "".join(job.stdout_parts)
            result["stderr"] = "".join(job.stderr_parts)
            return result

        result["stdout"] = "".join(job.stdout_parts)
        result["stderr"] = "".join(job.stderr_parts)
        result["success"] = job.success
        if job.error:
            result["error"] = job.error
        # Artifacts are populated by _finalize_background_artifacts when the
        # job reaches a terminal state; carry them here without download URLs
        # — URL composition happens in the MCP server layer where the public
        # base URL is known (matches the foreground path's contract).
        result["artifacts"] = list(job.artifacts)
        return result

    async def await_background_job(self, job_id: str) -> Optional[dict[str, Any]]:
        """Wait for a background job's task to reach a terminal state, then return its status.

        Returns ``None`` if the job was never registered or has already been purged.
        Used by the activity publisher to emit ``job_finished`` events; callers must
        treat exceptions on the underlying task as terminal (a failed task still
        leaves ``_BackgroundJob.status`` set by ``_collect_background_job``).
        """
        job = self._background_jobs.get(job_id)
        if job is None or job.task is None:
            return None
        try:
            await job.task
        except Exception:
            LOGGER.debug("Background job task raised for job_id=%s", job_id, exc_info=True)
        try:
            return self.check_background_job(job_id)
        except ValueError:
            return None

    def _get_kernel_execute_lock(self, session_id: str) -> asyncio.Lock:
        """Get-or-create the asyncio.Lock that serializes kernel access for *session_id*.

        A Jupyter kernel client has a single iopub queue; running ``kc.execute``
        and draining replies from two coroutines at once causes one coroutine to
        consume the other's ``status: idle`` reply, which leaves the loser
        spinning until its timeout fires.  Serializing here is correct because
        the kernel only processes one execute at a time anyway.
        """
        lock = self._kernel_execute_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._kernel_execute_locks[session_id] = lock
        return lock

    async def execute_code_for_session(
        self, session_id: str, code: str, timeout: float, working_dir: Optional[str] = None
    ) -> Tuple[str, str, bool, list[dict]]:
        """
        Execute code in the session's Jupyter kernel.

        Concurrent calls for the same ``session_id`` are serialized by a
        per-session asyncio.Lock so they cannot race on the shared
        Jupyter ``KernelClient`` iopub stream.

        Args:
            session_id: Session identifier
            code: Python code to execute
            timeout: Execution timeout in seconds
            working_dir: Optional working directory for kernel

        Returns:
            Tuple of ``(stdout, stderr, success, artifacts)`` where
            ``artifacts`` is a list of metadata dicts (one per new/modified
            file under the session's outputs dir) with shape
            ``{name, size_bytes, mime_type, modified_at, download_token}``.
            Each ``download_token`` is unguessable and valid for the
            session's lifetime; the MCP server composes the full URL.
        """
        # Look up session to get current user credentials, ensuring session
        # access goes through the manager (cleanup, expiry check, touch).
        try:
            session = self.get_session(session_id)
            user_token = session.user_token
            user_identity = session.user_identity
        except ValueError:
            return (
                "",
                (
                    f"Session {session_id} is no longer available (expired or cleaned up). "
                    "Please create a new session and retry."
                ),
                False,
                [],
            )

        running_job_id = self._get_running_job_for_session(session_id)
        if running_job_id:
            return "", f"Session busy — job {running_job_id} is still running", False, []

        async with self._get_kernel_execute_lock(session_id):
            return await self._execute_code_locked(
                session_id=session_id,
                code=code,
                timeout=timeout,
                working_dir=working_dir,
                user_token=user_token,
                user_identity=user_identity,
            )

    async def _execute_code_locked(
        self,
        *,
        session_id: str,
        code: str,
        timeout: float,
        working_dir: Optional[str],
        user_token: Optional[str],
        user_identity: Optional[str],
    ) -> Tuple[str, str, bool, list[dict]]:
        """Inner kernel-execution body; runs under :meth:`_get_kernel_execute_lock`."""
        km, kc = await self._get_or_create_kernel(
            session_id, working_dir, user_token=user_token, user_identity=user_identity
        )
        code = self._prepare_outputs_preamble(session_id) + \
            self._prepare_code_with_token_preamble(session_id, code, user_token)

        # Snapshot the outputs dir before executing so we can diff against
        # the post-execute state and surface only files this execute created
        # or modified.
        outputs_before = self._snapshot_outputs_dir(session_id)

        # Execute code
        msg_id = kc.execute(code)

        stdout_parts = []
        stderr_parts = []
        success = True

        start_time = time.monotonic()
        keepalive_interval_seconds = self.execution_session_keepalive_seconds
        last_keepalive = start_time - keepalive_interval_seconds

        # Collect output messages
        while True:
            now = time.monotonic()
            if now - last_keepalive >= keepalive_interval_seconds:
                if not self._touch_session_if_present(session_id):
                    return (
                        "",
                        (
                            f"Session {session_id} was cleaned up while execution was in progress. "
                            "Please create a new session and retry."
                        ),
                        False,
                        [],
                    )
                last_keepalive = now

            if time.monotonic() - start_time > timeout:
                await km.interrupt_kernel()
                await asyncio.sleep(1.0)
                return "", f"Execution timeout after {timeout}s", False, []

            try:
                msg = await kc.get_iopub_msg(timeout=1.0)
            except queue.Empty:
                continue

            msg_type = msg["msg_type"]
            content = msg.get("content", {})
            parent_id = msg.get("parent_header", {}).get("msg_id")

            # Only process messages from our execution
            if parent_id != msg_id:
                continue

            # Handle different message types
            if msg_type == "stream":
                stream_name = content.get("name", "stdout")
                text = content.get("text", "")
                if stream_name == "stdout":
                    stdout_parts.append(text)
                elif stream_name == "stderr":
                    stderr_parts.append(text)

            elif msg_type == "error":
                # Execution error
                traceback = "\n".join(content.get("traceback", []))
                stderr_parts.append(traceback)
                success = False

            elif msg_type == "execute_result":
                # Result of expression
                data = content.get("data", {})
                text_result = data.get("text/plain", "")
                if text_result:
                    stdout_parts.append(text_result)

            elif msg_type == "status":
                # Execution state changed
                state = content.get("execution_state")
                if state == "idle":
                    # Execution complete — drain any remaining iopub
                    # messages that may still be in-flight (e.g. a
                    # stdout stream message produced by print() that
                    # arrived after the idle status).
                    while True:
                        try:
                            trailing_msg = await asyncio.wait_for(kc.get_iopub_msg(timeout=0.2), timeout=0.5)
                        except (queue.Empty, asyncio.TimeoutError, Exception):
                            break
                        r_type = trailing_msg["msg_type"]
                        r_content = trailing_msg.get("content", {})
                        r_parent = trailing_msg.get("parent_header", {}).get("msg_id")
                        if r_parent != msg_id:
                            continue
                        if r_type == "stream":
                            txt = r_content.get("text", "")
                            if r_content.get("name") == "stdout":
                                stdout_parts.append(txt)
                            elif r_content.get("name") == "stderr":
                                stderr_parts.append(txt)
                        elif r_type == "execute_result":
                            data = r_content.get("data", {})
                            t = data.get("text/plain", "")
                            if t:
                                stdout_parts.append(t)
                    break

        stdout = "".join(stdout_parts)
        stderr = "".join(stderr_parts)

        self._kernel_last_used[session_id] = time.time()

        # Diff outputs dir post-execute and register any new/modified files
        # as artifacts.  Failures here must not fail the execute itself —
        # the kernel already ran successfully; artifact surfacing is best-
        # effort observability.
        try:
            outputs_after = self._snapshot_outputs_dir(session_id)
            artifacts = self._register_artifacts_from_diff(
                session_id, outputs_before, outputs_after
            )
        except Exception:
            LOGGER.warning(
                "Artifact discovery failed for session %s", session_id, exc_info=True
            )
            artifacts = []

        return stdout, stderr, success, artifacts

    async def _shutdown_kernel(self, session_id: str):
        """Shutdown and remove a kernel."""
        running_job_id = self._get_running_job_for_session(session_id)
        if running_job_id:
            job = self._background_jobs.get(running_job_id)
            if job:
                if job.task and not job.task.done():
                    job.task.cancel()
                    try:
                        await job.task
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        LOGGER.debug("Background job task cleanup failed during kernel shutdown", exc_info=True)
                job.success = False
                job.error = f"Kernel for session {session_id} was shut down while job {running_job_id} was running"
                job.status = "failed"
                self._mark_job_finished(job)

        if session_id in self._kernels:
            km, kc = self._kernels[session_id]
            LOGGER.info(f"Shutting down kernel for session {session_id}")

            try:
                kc.stop_channels()
                await km.shutdown_kernel(now=True)
                await km.cleanup_resources()
            except Exception as e:
                LOGGER.error(f"Error shutting down kernel for {session_id}: {e}")

            del self._kernels[session_id]
            del self._kernel_last_used[session_id]
            self._kernel_tokens.pop(session_id, None)
            self._kernel_execute_locks.pop(session_id, None)
            self._cleanup_session_artifacts(session_id)

    async def cleanup_idle_kernels(self, max_idle_time: float = 3600.0):
        """Cleanup kernels that have been idle for too long."""
        now = time.time()
        idle_sessions = [sid for sid, last_used in self._kernel_last_used.items() if now - last_used > max_idle_time]

        for session_id in idle_sessions:
            LOGGER.info(f"Cleaning up idle kernel for session {session_id}")
            await self._shutdown_kernel(session_id)

    # ========================================================================
    # Session Listing and Cleanup
    # ========================================================================

    def list_sessions(self) -> list[dict[str, Any]]:
        """
        List all active sessions with metadata.

        Returns:
            List of session info dicts
        """
        self._maybe_cleanup()

        sessions = self.storage.list_all()

        result = []
        for session in sessions.values():
            result.append(session.get_info())

        return result

    def _maybe_cleanup(self):
        """Run cleanup if interval has elapsed."""
        now = datetime.now()
        if now - self._last_cleanup > self.config.cleanup_interval:
            self._cleanup_expired()
            self._last_cleanup = now

    def _cleanup_expired(self):
        """Remove expired sessions and their kernels."""
        now = datetime.now()
        sessions = self.storage.list_all()

        expired = [
            session_id for session_id, session in sessions.items() if now - session.last_accessed > self.config.timeout
        ]

        for session_id in expired:
            # Shutdown kernel first
            if session_id in self._kernels:
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        asyncio.create_task(self._shutdown_kernel(session_id))
                    else:
                        loop.run_until_complete(self._shutdown_kernel(session_id))
                except Exception as e:
                    LOGGER.error(f"Failed to shutdown kernel for expired session {session_id}: {e}")

            session = self.storage.retrieve(session_id)
            if session:
                cleanup_failed = False
                try:
                    session.cleanup()
                except Exception as e:
                    cleanup_failed = True
                    LOGGER.error(
                        f"Error during cleanup of expired session {session_id}: {e}. "
                        f"Session will still be removed to prevent accumulation, but resources may be leaked."
                    )

                self.storage.delete(session_id)
                if cleanup_failed:
                    LOGGER.warning(f"Removed expired session {session_id} with failed cleanup")
                else:
                    LOGGER.info(f"Cleaned up expired session {session_id}")

    def _enforce_max_sessions(self):
        """
        Reject new session creation if the limit has been reached.

        Raises:
            MaxSessionsReachedError: If the number of active sessions is at or above
                the configured maximum.  The caller should surface this as
                a user-visible error so the client can close an existing
                session and retry.
        """
        if self.storage.count() >= self.config.max_sessions:
            raise MaxSessionsReachedError(
                f"Maximum number of sessions ({self.config.max_sessions}) reached. "
                f"Please close an existing session before creating a new one."
            )
