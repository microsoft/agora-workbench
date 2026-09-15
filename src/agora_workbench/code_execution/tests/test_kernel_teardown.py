"""Kernel teardown must claim its target atomically and be awaitable.

``_shutdown_kernel`` is a coroutine, and its callers used to schedule it as a
bare ``create_task`` and return.  Because it also removed the session from
``_kernels`` *after* its awaits, the registry advertised a kernel that was
already being destroyed for the whole teardown window: a concurrent execute
was handed the dying kernel, a second close scheduled a duplicate teardown
that crashed on the already-deleted key, and a teardown that resumed late
could evict whatever kernel occupied that session id by then.

These tests pin the fix: the kernel and all of its registry state are claimed
in one synchronous step before the first await, teardowns coalesce onto a
single referenced task, and callers that need the resources actually released
can wait for it.

See https://github.com/microsoft/agora-workbench/issues/314.
"""

import asyncio
import logging
import threading
import time
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest

from ..server import CodeExecutionServer
from ..sessions.manager import KERNEL_BOOTSTRAP_TOOL_PROXIES, SessionConfig, SessionManager, _BackgroundJob
from ..sessions.session import Session
from ..sessions.storage import InMemoryStorage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class StubKernelManager:
    """Kernel manager whose shutdown can be held open mid-flight."""

    def __init__(self, name: str = "k", gate: "asyncio.Event | None" = None):
        self.name = name
        self.gate = gate
        self.shutdown_started = False
        self.shutdown_finished = False

    async def shutdown_kernel(self, now: bool = False) -> None:
        self.shutdown_started = True
        if self.gate is not None:
            await self.gate.wait()
        self.shutdown_finished = True

    async def cleanup_resources(self) -> None:
        pass


class StubKernelClient:
    def __init__(self, name: str = "k"):
        self.name = name
        self.channels_stopped = False

    def stop_channels(self) -> None:
        self.channels_stopped = True


@pytest.fixture
def manager(tmp_path, monkeypatch) -> SessionManager:
    """A SessionManager whose outputs dir is redirected away from ``~``."""
    from .. import sessions as sessions_pkg

    monkeypatch.setattr(sessions_pkg.manager, "_OUTPUTS_BASE_DIR", tmp_path)
    return sessions_pkg.SessionManager()


def register_kernel(manager: SessionManager, session_id: str, name: str = "k", gate=None):
    """Install a stub kernel the way ``_get_or_create_kernel`` would."""
    km, kc = StubKernelManager(name, gate), StubKernelClient(name)
    manager._kernels[session_id] = cast(Any, (km, kc))
    manager._kernel_last_used[session_id] = 0.0
    manager._kernel_tokens[session_id] = "token"
    manager._kernel_execute_locks[session_id] = asyncio.Lock()
    manager._assign_kernel_generation(session_id)
    manager._kernel_session_generations[session_id] = manager._session_generations.get(session_id)
    return km, kc


async def let_teardown_start():
    """Yield enough for a scheduled teardown to reach its first await."""
    for _ in range(5):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# The kernel is claimed before the first await
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAtomicClaim:
    async def test_registry_entry_is_gone_before_teardown_suspends(self, manager):
        """The window in which the registry advertised a dying kernel is what
        made problems 2, 3 and 4 possible; it must not exist at all."""
        gate = asyncio.Event()
        km, _ = register_kernel(manager, "s1", gate=gate)

        task = asyncio.create_task(manager._shutdown_kernel("s1"))
        await let_teardown_start()

        assert km.shutdown_started, "teardown should be in flight"
        assert km.shutdown_finished is False
        assert "s1" not in manager._kernels
        assert "s1" not in manager._kernel_last_used
        assert "s1" not in manager._kernel_tokens
        assert "s1" in manager._kernel_execute_locks
        assert manager.get_kernel_generation("s1") is None

        gate.set()
        _ = await task

    async def test_second_teardown_claims_nothing(self, manager):
        gate = asyncio.Event()
        km_first, _ = register_kernel(manager, "s1", name="FIRST", gate=gate)

        first = asyncio.create_task(manager._shutdown_kernel("s1"))
        await let_teardown_start()

        # A second teardown for the same session finds nothing to claim.
        await manager._shutdown_kernel("s1")

        gate.set()
        _ = await first
        assert km_first.shutdown_finished

    async def test_replacement_waits_for_old_kernel_shutdown(self, manager):
        gate = asyncio.Event()
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        register_kernel(manager, session_id, name="OLD", gate=gate)

        old_shutdown = manager.close_session(session_id)
        assert old_shutdown is not None
        await let_teardown_start()

        with pytest.raises(ValueError, match="still closing"):
            manager.create_session(
                data={},
                user_identity="u",
                user_token="replacement-token",
                token_claims={},
                session_id=session_id,
            )

        gate.set()
        _ = await old_shutdown
        manager.create_session(
            data={},
            user_identity="u",
            user_token="replacement-token",
            token_claims={},
            session_id=session_id,
        )

        assert session_id not in manager._kernels
        assert manager.storage.retrieve(session_id) is not None
        manager.close_session(session_id)

    async def test_stale_teardown_cannot_evict_a_newer_kernel(self, manager):
        """Regression: the late teardown used to delete whatever occupied the
        session id, orphaning a live kernel and deleting its outputs dir."""
        gate = asyncio.Event()
        register_kernel(manager, "s1", name="OLD", gate=gate)

        stale = asyncio.create_task(manager._shutdown_kernel("s1"))
        await let_teardown_start()

        # A replacement kernel arrives while the old teardown is still running.
        new_km, new_kc = register_kernel(manager, "s1", name="NEW")
        manager.mark_kernel_bootstrapped("s1", KERNEL_BOOTSTRAP_TOOL_PROXIES)
        new_generation = manager.get_kernel_generation("s1")

        gate.set()
        _ = await stale

        assert manager._kernels.get("s1") == (new_km, new_kc), "live kernel was evicted"
        assert manager.get_kernel_generation("s1") == new_generation
        assert manager.is_kernel_bootstrapped("s1", KERNEL_BOOTSTRAP_TOOL_PROXIES) is True
        assert new_kc.channels_stopped is False, "live kernel was shut down by a stale teardown"

    async def test_shutdown_without_a_kernel_is_a_noop(self, manager, tmp_path):
        """A teardown that claims nothing must touch nothing.

        Returning early is what stops a *second*, stale teardown from failing a
        live background job or deleting a live session's artifacts out from
        under the kernel that legitimately owns the session id.
        """
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})

        # A live session with artifacts and a running job, but no kernel of its
        # own registered -- exactly the state a stale teardown would find.
        outputs = manager._get_outputs_dir(session_id)
        outputs.mkdir(parents=True, exist_ok=True)
        (outputs / "result.csv").write_text("data")

        job = _BackgroundJob(
            job_id="job-1",
            session_id=session_id,
            msg_id="m1",
            timeout=60.0,
            start_time=time.time(),
        )
        job.task = asyncio.create_task(asyncio.sleep(30))
        manager._background_jobs["job-1"] = job
        manager._session_running_jobs[session_id] = "job-1"

        await manager._shutdown_kernel(session_id)

        assert (outputs / "result.csv").read_text() == "data", "stale teardown deleted a live session's artifacts"
        assert job.status == "running", "stale teardown failed a live background job"
        assert not job.task.cancelled() and not job.task.done(), "stale teardown cancelled a live background job"

        job.task.cancel()

        # And the fully-unknown-session case still does not raise.
        await manager._shutdown_kernel("never-existed")

    async def test_active_session_keeps_execute_lock_across_idle_kernel_teardown(self, manager):
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        register_kernel(manager, session_id)
        execute_lock = manager._get_kernel_execute_lock(session_id)

        await manager._shutdown_kernel(session_id, cleanup_artifacts=False)

        assert manager._get_kernel_execute_lock(session_id) is execute_lock

    async def test_direct_live_session_kernel_shutdown_cleans_owned_artifacts(self, manager):
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        register_kernel(manager, session_id)
        outputs = manager._get_outputs_dir(session_id)
        (outputs / "result.csv").write_text("data")
        manager._session_artifacts[session_id] = {}

        await manager._shutdown_kernel(session_id)

        assert session_id not in manager._session_artifacts
        assert not outputs.exists()
        manager.close_session(session_id)

    async def test_closed_session_reclaims_unused_execute_lock(self, manager):
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        manager._get_kernel_execute_lock(session_id)

        manager.close_session(session_id)

        assert session_id not in manager._kernel_execute_locks

    async def test_caller_reaching_execute_lock_after_close_does_not_leak_it(self, manager):
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        manager.close_session(session_id)

        async with manager._kernel_execution_lock(session_id):
            assert session_id in manager._kernel_execute_locks

        assert session_id not in manager._kernel_execute_locks

    async def test_closed_session_reclaims_execute_lock_after_captured_callers_drain(self, manager):
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        holder_entered = asyncio.Event()
        release_holder = asyncio.Event()
        waiter_entered = asyncio.Event()
        release_waiter = asyncio.Event()

        async def holder():
            async with manager._kernel_execution_lock(session_id):
                holder_entered.set()
                await release_holder.wait()

        async def waiter():
            async with manager._kernel_execution_lock(session_id):
                waiter_entered.set()
                await release_waiter.wait()

        holder_task = asyncio.create_task(holder())
        await holder_entered.wait()
        execute_lock = manager._kernel_execute_locks[session_id]
        waiter_task = asyncio.create_task(waiter())
        await asyncio.sleep(0)

        manager.close_session(session_id)

        assert manager._kernel_execute_locks[session_id] is execute_lock
        release_holder.set()
        await waiter_entered.wait()
        assert manager._kernel_execute_locks[session_id] is execute_lock

        release_waiter.set()
        await asyncio.gather(holder_task, waiter_task)
        assert session_id not in manager._kernel_execute_locks

    async def test_replacement_session_keeps_lock_captured_before_close(self, manager):
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        release_holder = asyncio.Event()
        holder_entered = asyncio.Event()

        async def holder():
            async with manager._kernel_execution_lock(session_id):
                holder_entered.set()
                await release_holder.wait()

        holder_task = asyncio.create_task(holder())
        await holder_entered.wait()
        execute_lock = manager._kernel_execute_locks[session_id]
        manager.close_session(session_id)
        with pytest.raises(ValueError, match="still closing"):
            manager.create_session(
                data={},
                user_identity="replacement",
                user_token="t",
                token_claims={},
                session_id=session_id,
            )

        release_holder.set()
        assert await holder_task is None
        await manager.await_resource_cleanup()

        manager.create_session(
            data={},
            user_identity="replacement",
            user_token="t",
            token_claims={},
            session_id=session_id,
        )
        assert manager._kernel_execute_locks.get(session_id) is not execute_lock
        manager.close_session(session_id)
        assert session_id not in manager._kernel_execute_locks

    async def test_close_then_immediate_replacement_keeps_new_output_directory(self, manager, tmp_path):
        gate = asyncio.Event()
        session_id = "reused-session"
        session_dir = tmp_path / f"session_{session_id}"
        session_dir.mkdir()
        session_file = session_dir / "state.json"
        session_file.write_text("old")
        manager.create_session(
            data={"session_file": str(session_file)},
            user_identity="old",
            user_token="t",
            token_claims={},
            session_id=session_id,
        )
        register_kernel(manager, session_id, name="OLD", gate=gate)

        shutdown = manager.close_session(session_id)
        assert shutdown is not None
        with pytest.raises(ValueError, match="still closing"):
            manager.create_session(
                data={"session_file": str(session_file)},
                user_identity="new",
                user_token="replacement-token",
                token_claims={},
                session_id=session_id,
            )

        gate.set()
        _ = await shutdown
        await manager.await_resource_cleanup()

        session_dir.mkdir(exist_ok=True)
        session_file.write_text("replacement")
        manager.create_session(
            data={"session_file": str(session_file)},
            user_identity="new",
            user_token="replacement-token",
            token_claims={},
            session_id=session_id,
        )
        outputs = manager._get_outputs_dir(session_id)
        marker = outputs / "replacement.txt"
        marker.write_text("replacement")

        assert marker.read_text() == "replacement", "stale teardown deleted a live session's artifacts"
        assert session_file.read_text() == "replacement", "stale cleanup deleted a live session file"

    async def test_idle_cleanup_generation_snapshot_preserves_replacement_outputs(self, manager, monkeypatch):
        session_id = manager.create_session(data={}, user_identity="old", user_token="t", token_claims={})
        register_kernel(manager, session_id, name="OLD")
        manager._kernel_last_used[session_id] = 0.0
        original_shutdown = manager._shutdown_kernel
        marker = manager._get_outputs_dir(session_id) / "replacement.txt"
        replacement_kernel = None

        async def replace_then_shutdown(closing_session_id, **kwargs):
            nonlocal replacement_kernel
            old_session = manager.storage.retrieve(closing_session_id)
            assert old_session is not None
            manager.storage.delete(closing_session_id)
            old_session.cleanup()
            manager.create_session(
                data={},
                user_identity="new",
                user_token="replacement-token",
                token_claims={},
                session_id=closing_session_id,
            )
            marker.write_text("replacement")
            replacement_kernel = register_kernel(manager, closing_session_id, name="NEW")
            await original_shutdown(closing_session_id, **kwargs)

        monkeypatch.setattr(manager, "_shutdown_kernel", replace_then_shutdown)

        await manager.cleanup_idle_kernels(max_idle_time=-1)

        assert marker.read_text() == "replacement"
        assert manager._kernels[session_id] == replacement_kernel

    async def test_idle_cleanup_joins_close_path_teardown_without_deadlock(self, manager):
        """Joining a close-path teardown must not happen under the sweep's execute lease.

        The close path schedules teardown with ``wait_for_executions=True``, so it
        waits for the very drain event this sweep's execute-lock lease is holding
        open. Awaiting the joined task inside the lease would deadlock both.
        """
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        km, _ = register_kernel(manager, session_id)
        lock = manager._get_kernel_execute_lock(session_id)

        await lock.acquire()
        cleanup = asyncio.create_task(manager.cleanup_idle_kernels(max_idle_time=-1))
        while manager._kernel_execute_lock_users.get(session_id, 0) == 0:
            await asyncio.sleep(0)

        close_task = manager.close_session(session_id)
        assert close_task is not None
        await let_teardown_start()
        assert km.shutdown_started is False, "close teardown should be waiting for executions to drain"

        lock.release()
        _ = await asyncio.wait_for(cleanup, timeout=5)
        _ = await asyncio.wait_for(asyncio.shield(close_task), timeout=5)

        assert km.shutdown_finished
        assert session_id not in manager._kernels

    async def test_idle_cleanup_rechecks_last_used_after_execution_lock(self, manager):
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        km, _ = register_kernel(manager, session_id)
        manager._kernel_last_used[session_id] = 0.0
        lock = manager._get_kernel_execute_lock(session_id)

        await lock.acquire()
        cleanup = asyncio.create_task(manager.cleanup_idle_kernels(max_idle_time=1))
        await asyncio.sleep(0)
        manager._kernel_last_used[session_id] = time.time()
        lock.release()
        _ = await cleanup

        assert manager._kernels[session_id][0] is km
        assert km.shutdown_started is False

    async def test_cancelling_idle_cleanup_does_not_cancel_kernel_teardown(self, manager):
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        gate = asyncio.Event()
        km, _ = register_kernel(manager, session_id, gate=gate)

        cleanup = asyncio.create_task(manager.cleanup_idle_kernels(max_idle_time=-1))
        await let_teardown_start()
        cleanup.cancel()
        with pytest.raises(asyncio.CancelledError):
            _ = await cleanup

        shutdown = cast(asyncio.Task[Any], manager._kernel_shutdown_tasks[session_id])
        assert km.shutdown_started
        assert not shutdown.cancelled()
        gate.set()
        _ = await shutdown

    async def test_idle_artifact_deletion_does_not_hold_lifecycle_lock(self, manager, monkeypatch):
        session_id = manager.create_session(data={}, user_identity="old", user_token="t", token_claims={})
        register_kernel(manager, session_id, name="OLD")
        manager._kernel_last_used[session_id] = 0.0
        outputs = manager._get_outputs_dir(session_id)
        (outputs / "result.txt").write_text("payload")
        lock_was_available = False

        def inspect_lock(path, ignore_errors):
            nonlocal lock_was_available
            del path, ignore_errors

            def acquire_lock():
                nonlocal lock_was_available
                lock_was_available = manager._session_lifecycle_lock.acquire(timeout=1)
                if lock_was_available:
                    manager._session_lifecycle_lock.release()

            worker = threading.Thread(target=acquire_lock)
            worker.start()
            worker.join()

        monkeypatch.setattr("agora_workbench.code_execution.sessions.manager.shutil.rmtree", inspect_lock)

        await manager.cleanup_idle_kernels(max_idle_time=-1)

        assert lock_was_available

    async def test_outputs_dir_of_a_replacement_kernel_survives(self, manager, tmp_path):
        """The stale teardown also used to rmtree the live session's artifacts."""
        gate = asyncio.Event()
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        register_kernel(manager, session_id, name="OLD", gate=gate)

        stale = asyncio.create_task(manager._shutdown_kernel(session_id))
        await let_teardown_start()

        outputs = manager._get_outputs_dir(session_id)
        outputs.mkdir(parents=True, exist_ok=True)
        (outputs / "result.csv").write_text("data")
        register_kernel(manager, session_id, name="NEW")

        gate.set()
        _ = await stale

        assert (outputs / "result.csv").exists()


# ---------------------------------------------------------------------------
# Teardowns coalesce onto one referenced task
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCoalescing:
    async def test_duplicate_explicit_session_id_does_not_replace_owned_resources(self, manager):
        session_id = "explicit-session"
        manager.create_session(
            data={"owner": "original"},
            user_identity="original",
            user_token="t",
            token_claims={},
            session_id=session_id,
        )
        original = manager.storage.retrieve(session_id)

        with pytest.raises(ValueError, match="already exists"):
            manager.create_session(
                data={"owner": "replacement"},
                user_identity="replacement",
                user_token="t",
                token_claims={},
                session_id=session_id,
            )

        assert manager.storage.retrieve(session_id) is original

    async def test_storage_delete_failure_releases_closing_session_tombstone(self, manager, monkeypatch, tmp_path):
        session_file = tmp_path / "session.json"
        session_file.write_text("active")
        session_id = manager.create_session(
            data={"session_file": str(session_file)},
            user_identity="user",
            user_token="t",
            token_claims={},
        )
        km, _ = register_kernel(manager, session_id)
        original_delete = manager.storage.delete
        failed = False

        def fail_once(closing_session_id):
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("delete failed")
            original_delete(closing_session_id)

        monkeypatch.setattr(manager.storage, "delete", fail_once)

        with pytest.raises(RuntimeError, match="delete failed"):
            manager.close_session(session_id)
        await let_teardown_start()

        assert manager.storage.retrieve(session_id) is not None
        assert manager._kernels[session_id][0] is km
        assert not km.shutdown_started
        assert session_id not in manager._kernel_shutdown_tasks
        assert session_id not in manager._closing_session_ids
        assert session_file.read_text() == "active"

        shutdown_task = manager.close_session(session_id)
        assert shutdown_task is not None
        await asyncio.wait_for(shutdown_task, timeout=1)
        assert manager.storage.retrieve(session_id) is None

    async def test_session_file_cleanup_failure_releases_id_without_deleting_replacement(
        self, manager, monkeypatch, tmp_path
    ):
        session_file = tmp_path / "session.json"
        session_file.write_text("active")
        session_id = "explicit-session"
        manager.create_session(
            data={"session_file": str(session_file)},
            user_identity="user",
            user_token="t",
            token_claims={},
            session_id=session_id,
        )
        session = manager.storage.retrieve(session_id)
        assert session is not None

        monkeypatch.setattr(
            session,
            "_remove_session_file",
            lambda: (_ for _ in ()).throw(PermissionError("locked")),
        )

        manager.close_session(session_id)
        await manager.await_resource_cleanup()

        # Removal failed, so the old session gives up instead of pinning the ID
        # forever; it never retries, so a replacement file stays safe.
        assert session_id not in manager._closing_session_ids
        assert session_file.exists()
        replacement_file = tmp_path / "replacement.json"
        replacement_file.write_text("replacement")
        manager.create_session(
            data={"session_file": str(replacement_file)},
            user_identity="replacement",
            user_token="t",
            token_claims={},
            session_id=session_id,
        )
        session.claim_session_file_cleanup()
        assert replacement_file.exists()

    async def test_close_claim_is_atomic_with_explicit_id_replacement(self, manager):
        class PausingStorage(InMemoryStorage):
            def __init__(self):
                super().__init__()
                self.pause_retrieve = False
                self.retrieve_started = threading.Event()
                self.resume_retrieve = threading.Event()

            def retrieve(self, session_id):
                session = super().retrieve(session_id)
                if self.pause_retrieve:
                    self.retrieve_started.set()
                    self.resume_retrieve.wait(timeout=5)
                return session

        storage = PausingStorage()
        manager.storage = storage
        session_id = manager.create_session(data={}, user_identity="old", user_token="t", token_claims={})
        storage.pause_retrieve = True

        close_task = asyncio.create_task(asyncio.to_thread(manager.close_session, session_id))
        assert await asyncio.to_thread(storage.retrieve_started.wait, 5)
        replacement_task = asyncio.create_task(
            asyncio.to_thread(
                manager.create_session,
                {},
                "new",
                "t",
                {},
                None,
                session_id,
            )
        )
        await asyncio.sleep(0.05)
        assert not replacement_task.done()

        storage.pause_retrieve = False
        storage.resume_retrieve.set()
        _ = await close_task
        replacement_id = await replacement_task
        assert replacement_id == session_id

        assert manager.storage.retrieve(session_id).user_identity == "new"

    async def test_slow_artifact_cleanup_does_not_block_unrelated_session_creation(self, manager, monkeypatch):
        cleanup_started = threading.Event()
        resume_cleanup = threading.Event()
        session_id = manager.create_session(data={}, user_identity="old", user_token="t", token_claims={})

        def slow_cleanup(closing_session_id):
            assert closing_session_id == session_id
            cleanup_started.set()
            resume_cleanup.wait(timeout=5)

        monkeypatch.setattr(manager, "_cleanup_session_artifacts", slow_cleanup)
        close_task = asyncio.create_task(asyncio.to_thread(manager.close_session, session_id))
        assert await asyncio.to_thread(cleanup_started.wait, 5)

        replacement = await asyncio.wait_for(
            asyncio.to_thread(manager.create_session, {}, "other", "t", {}),
            timeout=1,
        )
        resume_cleanup.set()
        close_result = await close_task
        if close_result is not None:
            _ = await close_result

        assert manager.storage.retrieve(replacement) is not None

    async def test_double_close_does_not_raise(self, manager):
        """Regression: the loser used to die on ``del self._kernels[...]``
        inside a task nobody was watching."""
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        gate = asyncio.Event()
        register_kernel(manager, session_id, gate=gate)

        first = manager.close_session(session_id)
        second = manager.close_session(session_id)

        gate.set()
        for task in (first, second):
            if task is not None:
                _ = await task
        assert first is not None
        assert second is first, "the second close should join the in-flight teardown"
        assert first.exception() is None

    async def test_scheduling_returns_the_in_flight_task(self, manager):
        gate = asyncio.Event()
        register_kernel(manager, "s1", gate=gate)

        first = manager._schedule_kernel_shutdown("s1")
        second = manager._schedule_kernel_shutdown("s1")
        assert first is second

        gate.set()
        _ = await first

    async def test_task_is_referenced_while_running_and_released_after(self, manager):
        gate = asyncio.Event()
        register_kernel(manager, "s1", gate=gate)

        task = manager._schedule_kernel_shutdown("s1")
        await let_teardown_start()
        assert manager._kernel_shutdown_tasks.get("s1") is task, "an unreferenced task can be GC-ed mid-flight"

        gate.set()
        _ = await task
        await asyncio.sleep(0)
        assert "s1" not in manager._kernel_shutdown_tasks

    async def test_scheduling_without_a_kernel_returns_none(self, manager):
        assert manager._schedule_kernel_shutdown("never-existed") is None

    async def test_teardown_failure_is_logged_not_swallowed(self, manager, caplog):
        """A bare create_task surfaces failures only as 'Task exception was
        never retrieved', if at all."""

        class Exploding(StubKernelManager):
            async def cleanup_resources(self):
                raise RuntimeError("boom")

        manager._kernels["s1"] = (Exploding(), StubKernelClient())
        manager._kernel_last_used["s1"] = 0.0
        manager._assign_kernel_generation("s1")

        # _shutdown_kernel already guards the shutdown calls; force a failure
        # outside that guard to exercise the done-callback.
        async def failing(session_id, **kwargs):
            del kwargs
            raise RuntimeError("boom")

        manager._shutdown_kernel = failing
        with caplog.at_level(logging.ERROR):
            task = manager._schedule_kernel_shutdown("s1")
            assert task is not None
            with pytest.raises(RuntimeError):
                _ = await task

        assert any("Kernel shutdown for session s1 failed" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Callers can wait for the resources to actually be released
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAwaitableClose:
    async def test_close_session_returns_before_the_kernel_is_gone(self, manager):
        """Documents the sync behaviour that made 'cleanup done' misleading."""
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        gate = asyncio.Event()
        km, _ = register_kernel(manager, session_id, gate=gate)

        task = manager.close_session(session_id)
        assert km.shutdown_finished is False

        gate.set()
        assert task is not None
        _ = await task
        assert km.shutdown_finished is True

    async def test_aclose_session_waits_for_the_kernel(self, manager):
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        km, _ = register_kernel(manager, session_id)

        await manager.aclose_session(session_id)

        assert km.shutdown_finished is True
        assert session_id not in manager._kernels

    async def test_aclose_session_without_a_kernel_is_a_noop(self, manager):
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        await manager.aclose_session(session_id)

    async def test_aclose_session_claims_kernel_before_blocked_resource_cleanup(self, manager):
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        km, _ = register_kernel(manager, session_id)
        cleanup_gate = asyncio.Event()
        cleanup_started = asyncio.Event()
        session = manager.storage.retrieve(session_id)

        async def blocked_cleanup():
            cleanup_started.set()
            await cleanup_gate.wait()

        session.aclose = blocked_cleanup
        close_task = asyncio.create_task(manager.aclose_session(session_id))
        await cleanup_started.wait()

        assert session_id not in manager._kernels
        assert km.shutdown_finished is True
        assert manager.storage.retrieve(session_id) is None

        cleanup_gate.set()
        _ = await close_task
        assert manager.storage.retrieve(session_id) is None

    async def test_aclose_session_waits_for_active_session_resource_operation(self, manager):
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        session = manager.get_session(session_id)
        cleanup_started = asyncio.Event()
        original_aclose = session.aclose

        async def observed_cleanup():
            cleanup_started.set()
            await original_aclose()

        session.aclose = observed_cleanup
        operation = manager.session_resource_operation(session_id)
        await operation.__aenter__()
        close_task = asyncio.create_task(manager.aclose_session(session_id))
        await asyncio.sleep(0)

        assert not cleanup_started.is_set()
        assert not close_task.done()
        with pytest.raises(ValueError, match="not found or is closing"):
            async with manager.session_resource_operation(session_id):
                pass
        await operation.__aexit__(None, None, None)
        _ = await close_task
        assert cleanup_started.is_set()

    async def test_sync_close_keeps_session_file_until_resource_operation_finishes(self, manager, tmp_path):
        session_file = tmp_path / "session.json"
        session_file.write_text("active")
        session_id = manager.create_session(
            data={"session_file": str(session_file)},
            user_identity="u",
            user_token="t",
            token_claims={},
        )
        operation = manager.session_resource_operation(session_id)
        await operation.__aenter__()

        manager.close_session(session_id)

        assert session_file.read_text() == "active"

        await operation.__aexit__(None, None, None)
        await manager.await_resource_cleanup()
        assert not session_file.exists()

    async def test_close_session_waits_for_active_kernel_execution(self, manager):
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        km, _ = register_kernel(manager, session_id)
        session = manager.get_session(session_id)
        cleanup_started = asyncio.Event()
        original_aclose = session.aclose

        async def observed_cleanup():
            cleanup_started.set()
            await original_aclose()

        session.aclose = observed_cleanup
        operation = manager._kernel_execution_lock(session_id)
        await operation.__aenter__()
        shutdown = manager.close_session(session_id)
        await asyncio.sleep(0)

        assert shutdown is not None
        assert not shutdown.done()
        assert not km.shutdown_finished
        assert not cleanup_started.is_set()

        await operation.__aexit__(None, None, None)
        _ = await shutdown
        await manager.await_resource_cleanup()
        assert km.shutdown_finished
        assert cleanup_started.is_set()

    async def test_resource_operation_rejects_session_already_closing(self, manager):
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        register_kernel(manager, session_id)
        operation = manager.session_resource_operation(session_id)
        manager.close_session(session_id)

        with pytest.raises(ValueError, match="closing"):
            await operation.__aenter__()

    async def test_resource_operation_rejects_closed_or_replaced_session(self, manager):
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        operation = manager.session_resource_operation(session_id)
        manager.close_session(session_id)
        await manager.aclose_session(session_id)
        manager.create_session(
            data={},
            user_identity="replacement",
            user_token="t",
            token_claims={},
            session_id=session_id,
        )

        with pytest.raises(ValueError, match="not found or is closing"):
            await operation.__aenter__()

    async def test_resource_operation_adopts_generation_for_restored_session(self, manager):
        session_id = "restored-resource-operation"
        manager.storage.store(session_id, Session(session_id, {}, "default", "user", "token", {}))
        assert session_id not in manager._session_generations

        async with manager.session_resource_operation(session_id):
            assert manager._session_generations[session_id] > 0

        await manager.aclose_session(session_id)

    async def test_direct_kernel_execution_holds_session_resources(self, manager, monkeypatch):
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        session = manager.get_session(session_id)
        execution_started = asyncio.Event()
        release_execution = asyncio.Event()
        manager_closed = asyncio.Event()

        class DataManager:
            async def aclose(self):
                manager_closed.set()

        async def blocked_execution(*_args):
            execution_started.set()
            await release_execution.wait()
            return "", "", True, [], []

        session.data_manager = DataManager()
        monkeypatch.setattr(manager, "_execute_code_for_admitted_session", blocked_execution)

        execution = asyncio.create_task(manager.execute_code_for_session(session_id, "pass", 30))
        await execution_started.wait()
        close = asyncio.create_task(manager.aclose_session(session_id))
        await asyncio.sleep(0)

        assert not close.done()
        assert not manager_closed.is_set()

        release_execution.set()
        assert (await execution)[2]
        assert await close is None
        assert manager_closed.is_set()

    async def test_resource_operation_holds_explicit_id_until_deferred_cleanup_finishes(self, manager):
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        session = manager.get_session(session_id)
        outputs = manager._get_outputs_dir(session_id)
        (outputs / "result.csv").write_text("data")
        manager._session_artifacts[session_id] = {}
        cleanup_started = asyncio.Event()
        cleanup_gate = asyncio.Event()

        async def blocked_cleanup():
            cleanup_started.set()
            await cleanup_gate.wait()

        session.aclose = blocked_cleanup
        operation = manager.session_resource_operation(session_id)
        await operation.__aenter__()
        manager.close_session(session_id)

        with pytest.raises(ValueError, match="still closing"):
            manager.create_session(
                data={},
                user_identity="replacement",
                user_token="t",
                token_claims={},
                session_id=session_id,
            )

        await operation.__aexit__(None, None, None)
        await cleanup_started.wait()
        with pytest.raises(ValueError, match="still closing"):
            manager.create_session(
                data={},
                user_identity="replacement",
                user_token="t",
                token_claims={},
                session_id=session_id,
            )

        cleanup_gate.set()
        await manager.await_resource_cleanup()
        assert session_id not in manager._session_artifacts
        assert not outputs.exists()
        replacement = manager.create_session(
            data={},
            user_identity="replacement",
            user_token="t",
            token_claims={},
            session_id=session_id,
        )
        assert replacement == session_id
        manager.close_session(session_id)

    async def test_aclose_session_joins_deferred_resource_cleanup(self, manager):
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        session = manager.get_session(session_id)
        cleanup_started = asyncio.Event()
        original_aclose = session.aclose

        async def observed_cleanup():
            cleanup_started.set()
            await original_aclose()

        session.aclose = observed_cleanup
        operation = manager.session_resource_operation(session_id)
        await operation.__aenter__()
        manager.close_session(session_id)

        close_task = asyncio.create_task(manager.aclose_session(session_id))
        await asyncio.sleep(0)
        assert not close_task.done()
        assert not cleanup_started.is_set()

        await operation.__aexit__(None, None, None)
        _ = await close_task
        assert cleanup_started.is_set()

    async def test_aclose_session_joins_sync_scheduled_async_cleanup(self, manager):
        cleanup_started = asyncio.Event()
        cleanup_gate = asyncio.Event()

        class AsyncResource:
            def __init__(self):
                self.close_calls = 0

            async def aclose(self):
                self.close_calls += 1
                cleanup_started.set()
                await cleanup_gate.wait()

        resource = AsyncResource()
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        manager.get_session(session_id).data_manager = cast(Any, resource)
        manager.close_session(session_id)
        await cleanup_started.wait()

        close_task = asyncio.create_task(manager.aclose_session(session_id))
        await asyncio.sleep(0)
        assert not close_task.done()

        cleanup_gate.set()
        _ = await close_task
        assert resource.close_calls == 1

    async def test_aclose_all_sessions_joins_pending_resource_cleanup(self, manager):
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        operation = manager.session_resource_operation(session_id)
        await operation.__aenter__()
        manager.close_session(session_id)

        close_all = asyncio.create_task(manager.aclose_all_sessions())
        await asyncio.sleep(0)
        assert not close_all.done()

        await operation.__aexit__(None, None, None)
        _ = await close_all

    async def test_thread_close_defers_resource_cleanup_until_operation_drains(self, manager):
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        session = manager.get_session(session_id)
        cleanup_started = asyncio.Event()
        original_aclose = session.aclose

        async def observed_cleanup():
            cleanup_started.set()
            await original_aclose()

        session.aclose = observed_cleanup
        operation = manager.session_resource_operation(session_id)
        await operation.__aenter__()
        assert await asyncio.to_thread(manager.close_session, session_id) is None
        assert not cleanup_started.is_set()

        await operation.__aexit__(None, None, None)
        await manager.await_resource_cleanup()
        assert cleanup_started.is_set()

    async def test_aclose_all_sessions_cleans_independently_in_parallel(self, manager):
        session_ids = [
            manager.create_session(data={}, user_identity="u", user_token="t", token_claims={}) for _ in range(2)
        ]
        started = [asyncio.Event(), asyncio.Event()]
        gates = [asyncio.Event(), asyncio.Event()]
        for index, session_id in enumerate(session_ids):
            register_kernel(manager, session_id)
            session = manager.storage.retrieve(session_id)

            async def blocked_cleanup(i=index):
                started[i].set()
                await gates[i].wait()

            session.aclose = blocked_cleanup

        close_task = asyncio.create_task(manager.aclose_all_sessions())
        await asyncio.gather(*(event.wait() for event in started))
        assert all(session_id not in manager._kernels for session_id in session_ids)

        for gate in gates:
            gate.set()
        _ = await close_task
        assert manager.storage.count() == 0

    async def test_aclose_all_waits_for_already_closing_kernel(self, manager):
        gate = asyncio.Event()
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        kernel, _ = register_kernel(manager, session_id, gate=gate)
        shutdown = manager.close_session(session_id)
        assert shutdown is not None
        await let_teardown_start()

        close_all = asyncio.create_task(manager.aclose_all_sessions())
        await asyncio.sleep(0)
        assert not close_all.done()

        gate.set()
        _ = await close_all
        assert kernel.shutdown_finished

    async def test_cancelled_close_task_still_finishes_teardown_and_tombstone(self, manager):
        gate = asyncio.Event()
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        kernel, _ = register_kernel(manager, session_id, gate=gate)
        shutdown = manager.close_session(session_id)
        assert shutdown is not None
        await let_teardown_start()

        shutdown.cancel()
        await asyncio.sleep(0)
        assert not shutdown.done()
        gate.set()
        with pytest.raises(asyncio.CancelledError):
            _ = await shutdown

        assert kernel.shutdown_finished
        manager.create_session(
            data={},
            user_identity="replacement",
            user_token="t",
            token_claims={},
            session_id=session_id,
        )
        manager.close_session(session_id)

    async def test_aclose_all_skips_same_id_replacement_created_after_snapshot(self, manager, monkeypatch):
        session_id = manager.create_session(data={}, user_identity="old", user_token="t", token_claims={})
        original_close = manager.aclose_session
        replacement = None

        async def replace_then_close(closing_session_id, *, expected_generation=None):
            nonlocal replacement
            old_session = manager.storage.retrieve(closing_session_id)
            assert old_session is not None
            manager.storage.delete(closing_session_id)
            old_session.cleanup()
            manager.create_session(
                data={},
                user_identity="replacement",
                user_token="t",
                token_claims={},
                session_id=closing_session_id,
            )
            replacement = manager.storage.retrieve(closing_session_id)
            await original_close(closing_session_id, expected_generation=expected_generation)

        monkeypatch.setattr(manager, "aclose_session", replace_then_close)
        await manager.aclose_all_sessions()

        assert manager.storage.retrieve(session_id) is replacement

    async def test_aclose_all_adopts_restored_session_generation_before_close_snapshot(self, manager, monkeypatch):
        session_id = "restored-close-all"
        manager.storage.store(session_id, Session(session_id, {}, "default", "old", "t", {}))
        original_close = manager.aclose_session
        replacement = None

        async def replace_then_close(closing_session_id, *, expected_generation=None):
            nonlocal replacement
            assert expected_generation is not None
            old_session = manager.storage.retrieve(closing_session_id)
            assert old_session is not None
            manager.storage.delete(closing_session_id)
            old_session.cleanup()
            manager.create_session(
                data={},
                user_identity="replacement",
                user_token="t",
                token_claims={},
                session_id=closing_session_id,
            )
            replacement = manager.storage.retrieve(closing_session_id)
            await original_close(closing_session_id, expected_generation=expected_generation)

        monkeypatch.setattr(manager, "aclose_session", replace_then_close)
        await manager.aclose_all_sessions()

        assert manager.storage.retrieve(session_id) is replacement

    @pytest.mark.parametrize("async_close", [False, True])
    async def test_session_cleanup_attempts_every_resource_before_reporting(self, manager, tmp_path, async_close):
        attempted = []
        session_file = tmp_path / "session_cleanup.txt"
        session_file.write_text("payload")

        class FailingManager:
            def cleanup(self):
                attempted.append("manager")
                raise RuntimeError("manager failed")

            async def aclose(self):
                attempted.append("manager")
                raise RuntimeError("manager failed")

        class FailingExtension:
            def cleanup(self):
                attempted.append("extension")
                raise RuntimeError("extension failed")

            async def aclose(self):
                attempted.append("extension")
                raise RuntimeError("extension failed")

        class FailingPayload(dict):
            def cleanup(self):
                attempted.append("payload")
                raise RuntimeError("payload failed")

            async def aclose(self):
                attempted.append("payload")
                raise RuntimeError("payload failed")

        payload = FailingPayload(session_file=str(session_file))
        session_id = manager.create_session(
            payload,
            user_identity="u",
            user_token="t",
            token_claims={},
        )
        session = manager.get_session(session_id)
        session.data_manager = cast(Any, FailingManager())
        session.extensions["failing"] = FailingExtension()

        if async_close:
            with pytest.raises(ExceptionGroup, match="Session cleanup failed"):
                await manager.aclose_session(session_id)
        else:
            manager.close_session(session_id)
            with pytest.raises(ExceptionGroup, match="Session resource cleanup failed"):
                await manager.await_resource_cleanup()

        assert attempted == ["manager", "extension", "payload"]
        assert not session_file.exists()
        assert manager.storage.retrieve(session_id) is None

    @pytest.mark.parametrize("async_close", [False, True])
    async def test_cancelled_resource_cleanup_is_retried_after_session_removal(self, manager, async_close):
        attempts = 0

        class CancelsOnce:
            async def aclose(self):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise asyncio.CancelledError

        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        session = manager.get_session(session_id)
        session.data_manager = cast(Any, CancelsOnce())

        if async_close:
            with pytest.raises(asyncio.CancelledError):
                await manager.aclose_session(session_id)
        else:
            manager.close_session(session_id)
            with pytest.raises(asyncio.CancelledError):
                await manager.await_resource_cleanup()

        assert attempts == 2
        assert manager.storage.retrieve(session_id) is None

    async def test_cancelled_deferred_session_cleanup_transfers_retry_task(self, manager):
        retry_started = asyncio.Event()
        release_retry = asyncio.Event()
        attempts = 0

        class CancelsThenBlocks:
            async def aclose(self):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise asyncio.CancelledError
                retry_started.set()
                await release_retry.wait()

        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        session = manager.get_session(session_id)
        session.data_manager = cast(Any, CancelsThenBlocks())

        cleanup = asyncio.create_task(manager._cleanup_session_after_operations(session))
        await retry_started.wait()
        cleanup.cancel()
        cleanup_result = await asyncio.gather(cleanup, return_exceptions=True)
        assert isinstance(cleanup_result[0], asyncio.CancelledError)

        assert manager._session_owned_cleanup_tasks[session_id]
        release_retry.set()
        await manager.await_resource_cleanup()
        assert attempts == 2

    async def test_sync_close_retains_retry_after_synchronous_cancellation(self, manager):
        attempts = 0

        class CancelsOnce:
            def cleanup(self):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise asyncio.CancelledError

        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        session = manager.get_session(session_id)
        session.data_manager = cast(Any, CancelsOnce())

        manager.close_session(session_id)
        await manager.await_resource_cleanup()

        assert attempts == 2
        assert manager.storage.retrieve(session_id) is None

    async def test_aclose_session_reports_cancelled_owned_cleanup_task(self, manager):
        class CancelsAlways:
            async def aclose(self):
                raise asyncio.CancelledError

        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        manager.get_session(session_id).data_manager = cast(Any, CancelsAlways())

        manager.close_session(session_id)
        with pytest.raises(asyncio.CancelledError):
            await manager.aclose_session(session_id)

        assert manager.storage.retrieve(session_id) is None

    async def test_close_outside_event_loop_retries_cancelled_async_resource(self, manager):
        attempts = 0

        class CancelsOnce:
            async def aclose(self):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise asyncio.CancelledError

        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        manager.get_session(session_id).data_manager = cast(Any, CancelsOnce())

        await asyncio.to_thread(manager.close_session, session_id)

        assert attempts == 2
        assert manager.storage.retrieve(session_id) is None

    async def test_cancelled_resource_drain_remains_tracked_for_next_drain(self, manager):
        started = asyncio.Event()
        gate = asyncio.Event()
        finished = asyncio.Event()

        async def cleanup():
            started.set()
            await gate.wait()
            finished.set()

        task = asyncio.create_task(cleanup())
        manager._resource_cleanup_tasks.add(task)
        first_drain = asyncio.create_task(manager.await_resource_cleanup())
        await started.wait()

        first_drain.cancel()
        with pytest.raises(asyncio.CancelledError):
            _ = await first_drain
        assert task in manager._resource_cleanup_tasks
        assert not task.cancelled()

        second_drain = asyncio.create_task(manager.await_resource_cleanup())
        await asyncio.sleep(0)
        assert not second_drain.done()
        gate.set()
        _ = await second_drain

        assert finished.is_set()

    async def test_aclose_all_sessions_drains_through_repeated_cancellation(self, manager):
        gate = asyncio.Event()
        started = asyncio.Event()

        class Resource:
            async def aclose(self):
                started.set()
                await gate.wait()

        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        manager.get_session(session_id).data_manager = cast(Any, Resource())

        closing = asyncio.create_task(manager.aclose_all_sessions())
        await started.wait()
        closing.cancel()
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done()

        gate.set()
        with pytest.raises(asyncio.CancelledError):
            _ = await closing
        assert manager.storage.retrieve(session_id) is None
        assert not manager._resource_cleanup_tasks

    async def test_aclose_session_drains_through_repeated_cancellation(self, manager):
        gate = asyncio.Event()
        started = asyncio.Event()

        class Resource:
            async def aclose(self):
                started.set()
                await gate.wait()

        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        manager.get_session(session_id).data_manager = cast(Any, Resource())

        closing = asyncio.create_task(manager.aclose_session(session_id))
        await started.wait()
        closing.cancel()
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done()

        gate.set()
        with pytest.raises(asyncio.CancelledError):
            _ = await closing
        assert manager.storage.retrieve(session_id) is None
        assert not manager._resource_cleanup_tasks

    async def test_await_kernel_shutdown_is_a_noop_when_idle(self, manager):
        await manager.await_kernel_shutdown("never-existed")

    async def test_await_kernel_shutdown_does_not_cancel_teardown(self, manager):
        """A cancelled waiter must not take the teardown down with it."""
        gate = asyncio.Event()
        km, _ = register_kernel(manager, "s1", gate=gate)
        task = manager._schedule_kernel_shutdown("s1")
        assert task is not None

        waiter = asyncio.create_task(manager.await_kernel_shutdown("s1"))
        await let_teardown_start()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            _ = await waiter

        gate.set()
        _ = await task
        assert km.shutdown_finished is True


# ---------------------------------------------------------------------------
# A replacement kernel is not built alongside a dying one
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestKernelRebuildWaits:
    async def test_untracked_kernel_start_is_rejected_before_teardown_wait(self, manager, monkeypatch):
        shutdown_waited = False

        async def wait_for_shutdown(_session_id):
            nonlocal shutdown_waited
            shutdown_waited = True

        monkeypatch.setattr(manager, "await_kernel_shutdown", wait_for_shutdown)

        with pytest.raises(ValueError, match="does not exist"):
            await manager._get_or_create_kernel("s1")

        assert not shutdown_waited
        assert "s1" not in manager._kernels

    async def test_preexisting_storage_session_gets_kernel_generation(self, monkeypatch):
        from .. import sessions as sessions_pkg

        storage = InMemoryStorage()
        storage.store("restored", Session("restored", {}, "default", "user", "token", {}))
        manager = SessionManager(SessionConfig(storage_backend=storage))

        class FakeKernelManager:
            def __init__(self, kernel_name=None):
                self.kernel_name = kernel_name

            @property
            def kernel_spec(self):
                raise RuntimeError("no kernelspec in tests")

            async def start_kernel(self, env=None, cwd=None):
                pass

            def client(self):
                return FakeKernelClient()

            async def shutdown_kernel(self, now=False):
                pass

            async def cleanup_resources(self):
                pass

        class FakeKernelClient:
            def start_channels(self):
                pass

            async def wait_for_ready(self):
                pass

            def stop_channels(self):
                pass

        monkeypatch.setattr(sessions_pkg.manager, "AsyncKernelManager", FakeKernelManager)

        kernel = await manager._get_or_create_kernel("restored")

        assert kernel == manager._kernels["restored"]
        assert manager._session_generations["restored"] == manager._kernel_session_generations["restored"]
        await manager.aclose_session("restored")

    async def test_close_waits_for_unregistered_kernel_start(self, manager, monkeypatch):
        from .. import sessions as sessions_pkg

        start_entered = asyncio.Event()
        start_gate = asyncio.Event()

        class FakeKernelManager:
            shutdown_calls = 0
            cleanup_calls = 0

            def __init__(self, kernel_name=None):
                self.kernel_name = kernel_name

            @property
            def kernel_spec(self):
                raise RuntimeError("no kernelspec in tests")

            async def start_kernel(self, env=None, cwd=None):
                start_entered.set()
                await start_gate.wait()

            def client(self):
                return FakeKernelClient()

            async def shutdown_kernel(self, now=False):
                self.shutdown_calls += 1

            async def cleanup_resources(self):
                self.cleanup_calls += 1

        class FakeKernelClient:
            def start_channels(self):
                pass

            async def wait_for_ready(self):
                pass

            def stop_channels(self):
                pass

        monkeypatch.setattr(sessions_pkg.manager, "AsyncKernelManager", FakeKernelManager)
        session_id = manager.create_session(data={}, user_identity="user", user_token="token", token_claims={})
        startup = asyncio.create_task(manager._get_or_create_kernel(session_id))
        await start_entered.wait()

        shutdown = manager.close_session(session_id)
        assert shutdown is not None
        await asyncio.sleep(0)
        assert not shutdown.done()
        assert session_id in manager._closing_session_ids

        start_gate.set()
        with pytest.raises(ValueError, match="closed while its kernel was starting"):
            await startup
        await shutdown

        assert session_id not in manager._kernel_start_tasks
        assert session_id not in manager._kernels
        assert session_id not in manager._closing_session_ids

    @pytest.mark.parametrize("cancel_phase", ["start", "ready"])
    async def test_cancelled_kernel_start_cleans_partial_kernel(self, manager, monkeypatch, cancel_phase):
        from .. import sessions as sessions_pkg

        phase_entered = asyncio.Event()
        never = asyncio.Event()

        class FakeKernelManager:
            def __init__(self, kernel_name=None):
                self.kernel_name = kernel_name
                self.shutdown_calls = 0
                self.cleanup_calls = 0
                instances.append(self)

            @property
            def kernel_spec(self):
                raise RuntimeError("no kernelspec in tests")

            async def start_kernel(self, env=None, cwd=None):
                if cancel_phase == "start":
                    phase_entered.set()
                    await never.wait()

            def client(self):
                return client

            async def shutdown_kernel(self, now=False):
                self.shutdown_calls += 1

            async def cleanup_resources(self):
                self.cleanup_calls += 1

        class FakeKernelClient:
            def __init__(self):
                self.stop_calls = 0

            def start_channels(self):
                pass

            async def wait_for_ready(self):
                if cancel_phase == "ready":
                    phase_entered.set()
                    await never.wait()

            def stop_channels(self):
                self.stop_calls += 1

        instances = []
        client = FakeKernelClient()
        monkeypatch.setattr(sessions_pkg.manager, "AsyncKernelManager", FakeKernelManager)
        session_id = manager.create_session(data={}, user_identity="user", user_token="token", token_claims={})
        startup = asyncio.create_task(manager._get_or_create_kernel(session_id))
        await phase_entered.wait()

        startup.cancel()
        with pytest.raises(asyncio.CancelledError):
            await startup

        assert instances[0].shutdown_calls == 1
        assert instances[0].cleanup_calls == 1
        assert client.stop_calls == (1 if cancel_phase == "ready" else 0)
        assert session_id not in manager._kernel_start_tasks
        assert session_id not in manager._kernels

    async def test_get_or_create_waits_for_pending_teardown(self, manager, monkeypatch):
        from .. import sessions as sessions_pkg

        gate = asyncio.Event()
        manager.create_session(
            data={},
            user_identity="u",
            user_token="token",
            token_claims={},
            session_id="s1",
        )
        old_km, _ = register_kernel(manager, "s1", name="OLD", gate=gate)
        teardown = manager._schedule_kernel_shutdown("s1")
        assert teardown is not None
        await let_teardown_start()

        created_while_old_alive = []

        class FakeKernelManager:
            def __init__(self, kernel_name=None):
                self.kernel_name = kernel_name

            @property
            def kernel_spec(self):
                raise RuntimeError("no kernelspec in tests")

            async def start_kernel(self, env=None, cwd=None):
                created_while_old_alive.append(old_km.shutdown_finished)

            def client(self):
                return FakeKernelClient()

        class FakeKernelClient:
            def start_channels(self):
                pass

            async def wait_for_ready(self):
                pass

        monkeypatch.setattr(sessions_pkg.manager, "AsyncKernelManager", FakeKernelManager)

        create = asyncio.create_task(manager._get_or_create_kernel("s1"))
        await let_teardown_start()
        assert not create.done(), "kernel rebuild should wait for the teardown"

        gate.set()
        _ = await teardown
        _ = await create

        assert created_while_old_alive == [True], "replacement was built before the old kernel finished shutting down"
        assert "s1" in manager._kernels

    async def test_cancelled_kernel_start_drains_stale_generation_teardown(self, manager, monkeypatch):
        from .. import sessions as sessions_pkg

        gate = asyncio.Event()
        manager.create_session(
            data={},
            user_identity="old",
            user_token="token",
            token_claims={},
            session_id="s1",
        )
        old_kernel, _ = register_kernel(manager, "s1", name="OLD", gate=gate)
        manager.storage.delete("s1")
        manager.create_session(
            data={},
            user_identity="new",
            user_token="replacement-token",
            token_claims={},
            session_id="s1",
        )

        class UnexpectedKernelManager:
            def __init__(self, *args, **kwargs):
                raise AssertionError("replacement kernel started after request cancellation")

        monkeypatch.setattr(sessions_pkg.manager, "AsyncKernelManager", UnexpectedKernelManager)

        create = asyncio.create_task(manager._get_or_create_kernel("s1"))
        await let_teardown_start()
        assert old_kernel.shutdown_started
        create.cancel()
        await asyncio.sleep(0)
        assert not create.done()

        gate.set()
        with pytest.raises(asyncio.CancelledError):
            _ = await create

        assert old_kernel.shutdown_finished
        assert "s1" not in manager._kernels

    async def test_repeatedly_cancelled_kernel_start_still_drains_stale_teardown(self, manager, monkeypatch):
        """A second cancellation must not let the request return early.

        ``_shutdown_kernel`` removes the registry entry before its first await,
        so if the kernel-start request returns while the shared teardown is
        still in flight the old Jupyter kernel is left running with nothing to
        reclaim it. Draining must survive repeated cancellation, matching the
        canonical shielded drain loops elsewhere in this module.
        """
        from .. import sessions as sessions_pkg

        gate = asyncio.Event()
        manager.create_session(
            data={},
            user_identity="old",
            user_token="token",
            token_claims={},
            session_id="s1",
        )
        old_kernel, _ = register_kernel(manager, "s1", name="OLD", gate=gate)
        manager.storage.delete("s1")
        manager.create_session(
            data={},
            user_identity="new",
            user_token="replacement-token",
            token_claims={},
            session_id="s1",
        )

        class UnexpectedKernelManager:
            def __init__(self, *args, **kwargs):
                raise AssertionError("replacement kernel started before the stale teardown drained")

        monkeypatch.setattr(sessions_pkg.manager, "AsyncKernelManager", UnexpectedKernelManager)

        create = asyncio.create_task(manager._get_or_create_kernel("s1"))
        await let_teardown_start()
        assert old_kernel.shutdown_started

        # First cancellation: the caller parks awaiting the shared teardown.
        create.cancel()
        await asyncio.sleep(0)
        assert not create.done()
        assert not old_kernel.shutdown_finished

        # Second cancellation while still draining must not propagate before the
        # captured teardown completes.
        create.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not create.done(), "request returned before the stale teardown drained"
        assert not old_kernel.shutdown_finished

        gate.set()
        with pytest.raises(asyncio.CancelledError):
            _ = await create

        assert old_kernel.shutdown_finished
        assert "s1" not in manager._kernels

    async def test_cancelled_kernel_start_does_not_cancel_teardown_for_concurrent_waiter(self, manager, monkeypatch):
        """Cancelling one waiter must not cancel the shared teardown task.

        The teardown scheduled for a stale kernel is shared with every other
        consumer awaiting it. A cancelled kernel-start request must leave that
        task running so concurrent waiters still observe a clean completion.
        """
        from .. import sessions as sessions_pkg

        gate = asyncio.Event()
        manager.create_session(
            data={},
            user_identity="old",
            user_token="token",
            token_claims={},
            session_id="s1",
        )
        old_kernel, _ = register_kernel(manager, "s1", name="OLD", gate=gate)
        manager.storage.delete("s1")
        manager.create_session(
            data={},
            user_identity="new",
            user_token="replacement-token",
            token_claims={},
            session_id="s1",
        )

        class UnexpectedKernelManager:
            def __init__(self, *args, **kwargs):
                raise AssertionError("replacement kernel started after request cancellation")

        monkeypatch.setattr(sessions_pkg.manager, "AsyncKernelManager", UnexpectedKernelManager)

        create = asyncio.create_task(manager._get_or_create_kernel("s1"))
        await let_teardown_start()
        assert old_kernel.shutdown_started
        shared_teardown = manager._kernel_shutdown_tasks["s1"]

        # A second, independent consumer waits directly on the same task.
        async def co_wait() -> None:
            result = await shared_teardown
            assert result is None

        co_waiter = asyncio.create_task(co_wait())
        await asyncio.sleep(0)

        create.cancel()
        await asyncio.sleep(0)
        assert not shared_teardown.cancelled(), "a cancelled waiter cancelled the shared teardown"
        assert not co_waiter.done()

        gate.set()
        with pytest.raises(asyncio.CancelledError):
            _ = await create
        assert await co_waiter is None

        assert old_kernel.shutdown_finished
        assert not shared_teardown.cancelled()
        assert "s1" not in manager._kernels

    async def test_failed_kernel_start_rolls_back_unregistered_kernel(self, manager, monkeypatch):
        from .. import sessions as sessions_pkg

        session_id = manager.create_session(data={}, user_identity="user", user_token="token", token_claims={})

        class FailingKernelManager:
            started = False
            shut_down = False
            cleaned_up = False

            def __init__(self, kernel_name=None):
                self.kernel_name = kernel_name

            @property
            def kernel_spec(self):
                raise RuntimeError("no kernelspec in tests")

            async def start_kernel(self, env=None, cwd=None):
                self.started = True

            def client(self):
                return FailingKernelClient()

            async def shutdown_kernel(self, now=False):
                self.shut_down = True
                raise asyncio.CancelledError

            async def cleanup_resources(self):
                self.cleaned_up = True

        class FailingKernelClient:
            channels_stopped = False

            def start_channels(self):
                pass

            async def wait_for_ready(self):
                raise RuntimeError("kernel did not become ready")

            def stop_channels(self):
                self.channels_stopped = True
                raise RuntimeError("channel cleanup failed")

        kernel_manager = FailingKernelManager()
        monkeypatch.setattr(sessions_pkg.manager, "AsyncKernelManager", lambda **kwargs: kernel_manager)

        with pytest.raises(RuntimeError, match="did not become ready"):
            await manager._get_or_create_kernel(session_id)

        assert kernel_manager.started
        assert kernel_manager.shut_down
        assert kernel_manager.cleaned_up
        assert session_id not in manager._kernels

    async def test_idle_cleanup_registers_teardown_for_kernel_rebuild_waiters(self, manager):
        gate = asyncio.Event()
        session_id = manager.create_session(data={}, user_identity="user", user_token="t", token_claims={})
        register_kernel(manager, session_id, gate=gate)
        manager._kernel_last_used[session_id] = 0

        cleanup = asyncio.create_task(manager.cleanup_idle_kernels(max_idle_time=-1))
        await let_teardown_start()
        assert session_id in manager._kernel_shutdown_tasks

        gate.set()
        _ = await cleanup

    async def test_generation_mismatch_teardown_preserves_replacement_outputs(self, manager, monkeypatch):
        from .. import sessions as sessions_pkg

        manager.create_session(
            data={},
            user_identity="old",
            user_token="token",
            token_claims={},
            session_id="s1",
        )
        register_kernel(manager, "s1", name="OLD")
        old_session = manager.storage.retrieve("s1")
        assert old_session is not None
        manager.storage.delete("s1")
        old_session.cleanup()
        manager.create_session(
            data={},
            user_identity="new",
            user_token="replacement-token",
            token_claims={},
            session_id="s1",
        )
        outputs = manager._get_outputs_dir("s1")
        marker = outputs / "replacement.txt"
        marker.write_text("replacement")

        class FakeKernelManager:
            def __init__(self, kernel_name=None):
                self.kernel_name = kernel_name

            @property
            def kernel_spec(self):
                raise RuntimeError("no kernelspec in tests")

            async def start_kernel(self, env=None, cwd=None):
                pass

            def client(self):
                return FakeKernelClient()

        class FakeKernelClient:
            def start_channels(self):
                pass

            async def wait_for_ready(self):
                pass

        monkeypatch.setattr(sessions_pkg.manager, "AsyncKernelManager", FakeKernelManager)

        await manager._get_or_create_kernel("s1")

        assert marker.read_text() == "replacement"


# ---------------------------------------------------------------------------
# The sync fallback path
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNoRunningLoop:
    def test_close_session_outside_a_loop_warns_actionably(self, manager, caplog):
        """``asyncio.get_event_loop()`` raises outside a loop on Python >=3.12,
        so the old fallback degraded to a leak with a misleading message."""
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        register_kernel(manager, session_id)

        with caplog.at_level(logging.WARNING):
            result = manager.close_session(session_id)

        assert result is None
        messages = [r.getMessage() for r in caplog.records]
        assert any("no running event loop" in m for m in messages)
        assert any("aclose_session" in m for m in messages), "the warning should name the supported alternative"
        assert any("close_session()" in m for m in messages), "the warning should name the operation that failed"
        # The session itself is still removed, as before.
        assert manager.storage.retrieve(session_id) is None

    def test_expired_session_cleanup_outside_a_loop_warns_too(self, manager, caplog):
        """The expiry sweep leaks a kernel in exactly the same way, and is a
        *background* path -- nobody is watching it, so silence there
        accumulates invisibly. It must be as loud as ``close_session``."""
        manager.config.timeout = timedelta(seconds=-1)  # everything is already expired
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        register_kernel(manager, session_id)

        with caplog.at_level(logging.WARNING):
            manager._cleanup_expired()

        messages = [r.getMessage() for r in caplog.records]
        assert any("no running event loop" in m and session_id in m for m in messages), (
            "expired-session cleanup silently leaked the kernel"
        )
        assert any("Expired-session cleanup" in m for m in messages), (
            "the warning should name the sweep, not misattribute the leak to close_session()"
        )

    def test_expired_cleanup_skips_same_id_replacement_created_after_snapshot(self, manager, monkeypatch):
        manager.config.timeout = timedelta(seconds=-1)
        session_id = manager.create_session(data={}, user_identity="old", user_token="t", token_claims={})
        original_close = manager._close_session_sync
        replacement = None

        def replace_then_close(closing_session_id, *, caller, expected_generation=None):
            nonlocal replacement
            old_session = manager.storage.retrieve(closing_session_id)
            assert old_session is not None
            manager.storage.delete(closing_session_id)
            old_session.cleanup()
            manager.create_session(
                data={},
                user_identity="replacement",
                user_token="t",
                token_claims={},
                session_id=closing_session_id,
            )
            replacement = manager.storage.retrieve(closing_session_id)
            return original_close(
                closing_session_id,
                caller=caller,
                expected_generation=expected_generation,
            )

        monkeypatch.setattr(manager, "_close_session_sync", replace_then_close)
        manager._cleanup_expired()

        assert manager.storage.retrieve(session_id) is replacement

    def test_expired_restored_session_gets_generation_before_close_snapshot(self, manager, monkeypatch):
        manager.config.timeout = timedelta(seconds=-1)
        session_id = "restored-expired"
        manager.storage.store(session_id, Session(session_id, {}, "default", "old", "t", {}))
        original_close = manager._close_session_sync
        replacement = None

        def replace_then_close(closing_session_id, *, caller, expected_generation=None):
            nonlocal replacement
            assert expected_generation is not None
            manager.storage.delete(closing_session_id)
            manager.create_session(
                data={},
                user_identity="replacement",
                user_token="t",
                token_claims={},
                session_id=closing_session_id,
            )
            replacement = manager.storage.retrieve(closing_session_id)
            return original_close(
                closing_session_id,
                caller=caller,
                expected_generation=expected_generation,
            )

        monkeypatch.setattr(manager, "_close_session_sync", replace_then_close)
        manager._cleanup_expired()

        assert manager.storage.retrieve(session_id) is replacement

    def test_no_warning_when_there_is_simply_no_kernel(self, manager, caplog):
        """The benign ``None`` (nothing to tear down) must stay quiet, or the
        warning becomes noise operators learn to ignore."""
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})

        with caplog.at_level(logging.WARNING):
            manager.close_session(session_id)

        assert not [r for r in caplog.records if "no running event loop" in r.getMessage()]

    def test_leaked_kernel_is_still_reclaimable_from_async_code(self, manager, caplog):
        """The warning tells operators the kernel is recoverable via
        ``aclose_session``. That promise is only safe to print because the
        kernel stays registered under its session id -- teardown pops
        ``_kernels`` inside ``_shutdown_kernel``, which never ran here. If a
        future change dropped the registration alongside the session state,
        the advice would send people after a kernel nothing can reach."""
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        register_kernel(manager, session_id)

        with caplog.at_level(logging.WARNING):
            manager.close_session(session_id)

        assert session_id in manager._kernels, "the leaked kernel must remain reachable for recovery"
        assert any(f"aclose_session('{session_id}')" in r.getMessage() for r in caplog.records), (
            "the warning should name the exact recovery call, not just the method"
        )

        asyncio.run(manager.aclose_session(session_id))
        assert session_id not in manager._kernels, "the documented recovery path did not reclaim the kernel"

    async def test_aclose_all_reclaims_kernel_after_thread_close(self, manager):
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        kernel, _ = register_kernel(manager, session_id)

        await asyncio.to_thread(manager.close_session, session_id)
        assert session_id in manager._kernels

        await manager.aclose_all_sessions()

        assert kernel.shutdown_finished
        assert session_id not in manager._kernels
        assert session_id not in manager._closing_sessions

    async def test_aclose_all_reclaims_thread_close_after_initial_snapshot(self, manager):
        first_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        first_kernel, _ = register_kernel(manager, first_id)
        first_cleanup_started = asyncio.Event()
        first_cleanup_gate = asyncio.Event()
        first_session = manager.get_session(first_id)

        async def blocked_cleanup():
            first_cleanup_started.set()
            await first_cleanup_gate.wait()

        first_session.aclose = blocked_cleanup
        close_all = asyncio.create_task(manager.aclose_all_sessions())
        await first_cleanup_started.wait()

        second_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        second_kernel, _ = register_kernel(manager, second_id)
        await asyncio.to_thread(manager.close_session, second_id)
        first_cleanup_gate.set()
        close_result = await close_all

        assert close_result is None
        assert first_kernel.shutdown_finished
        assert second_kernel.shutdown_finished
        assert not manager._closing_sessions

    async def test_background_job_holds_session_resources_until_cancelled(self, manager, monkeypatch):
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        session = manager.get_session(session_id)
        _, kernel_client = register_kernel(manager, session_id)
        kernel_client.execute = lambda _code: "message-id"
        collector_started = asyncio.Event()
        collector_cancelled = asyncio.Event()
        manager_closed = asyncio.Event()

        class DataManager:
            async def aclose(self):
                manager_closed.set()

        session.data_manager = DataManager()

        async def blocked_collector(_job, _kernel_manager, _kernel_client):
            collector_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                assert not manager_closed.is_set()
                collector_cancelled.set()

        monkeypatch.setattr(manager, "_collect_background_job", blocked_collector)
        result = await manager.start_background_execution_for_session(session_id, "pass", 30)
        await collector_started.wait()

        await manager.aclose_session(session_id)

        assert result["status"] == "running"
        assert collector_cancelled.is_set()
        assert manager_closed.is_set()

    async def test_cancelled_unstarted_background_collector_releases_resource_lease(self, manager, monkeypatch):
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        release_started = asyncio.Event()
        release_gate = asyncio.Event()

        class ResourceOperation:
            async def __aexit__(self, exc_type, exc, traceback):
                del exc_type, exc, traceback
                release_started.set()
                await release_gate.wait()

        resource_operation = ResourceOperation()
        job = _BackgroundJob("job", session_id, "message", 30, time.monotonic())

        async def collector(*_args):
            pytest.fail("cancelled collector should not start")

        monkeypatch.setattr(manager, "_collect_background_job", collector)
        task = manager._start_background_job_collector(
            job,
            cast(Any, StubKernelManager()),
            cast(Any, StubKernelClient()),
            resource_operation,
        )
        task.cancel()
        _ = await asyncio.gather(task, return_exceptions=True)
        await release_started.wait()
        close = asyncio.create_task(manager.aclose_session(session_id))
        await asyncio.sleep(0)

        assert not close.done()

        release_gate.set()
        assert await close is None
        assert not manager._background_lease_release_tasks

    @pytest.mark.parametrize(
        ("method_name", "arguments"),
        [
            ("start_background_execution_for_session", ("pass", 30)),
            ("start_promoted_execution_for_session", ("pass", 30, 1)),
        ],
    )
    async def test_background_dispatch_holds_session_resources(self, manager, monkeypatch, method_name, arguments):
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        session = manager.get_session(session_id)
        dispatch_started = asyncio.Event()
        release_dispatch = asyncio.Event()
        manager_closed = asyncio.Event()

        class DataManager:
            async def aclose(self):
                manager_closed.set()

        session.data_manager = DataManager()

        async def blocked_dispatch(*_args):
            assert manager._session_resource_users[session_id] == 1
            dispatch_started.set()
            await release_dispatch.wait()
            return {"status": "running"}

        monkeypatch.setattr(manager, f"_{method_name}", blocked_dispatch)
        dispatch = asyncio.create_task(getattr(manager, method_name)(session_id, *arguments))
        await dispatch_started.wait()
        close = asyncio.create_task(manager.aclose_session(session_id))
        await asyncio.sleep(0)

        assert not close.done()
        assert not manager_closed.is_set()

        release_dispatch.set()
        assert await dispatch == {"status": "running"}
        assert await close is None
        assert manager_closed.is_set()


# ---------------------------------------------------------------------------
# Batch cleanup does not report success while kernels are still resident
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestParallelBatchCleanup:
    """Problem 1 in issue #314, at the call site that motivated it.

    ``_cleanup_parallel_batch_sessions`` used to call the fire-and-forget
    ``close_session`` for each child and return immediately, so a batch
    reported as cleaned up was still holding every child kernel -- and every
    child kernel's GPU memory. The method is exercised directly against a
    stand-in ``self`` to keep this file free of the session-scoped server
    fixture, which builds a real environment.
    """

    @staticmethod
    def _fake_server(manager: SessionManager, session_ids: list[str]) -> CodeExecutionServer:
        """The subset of server state the method under test actually reads."""
        job_ids = [f"job-{i}" for i in range(len(session_ids))]
        return cast(
            CodeExecutionServer,
            SimpleNamespace(
                session_manager=manager,
                _parallel_state_lock=asyncio.Lock(),
                _parallel_batches={"b1": {"job_ids": job_ids, "cleanup_done": False}},
                _parallel_jobs={jid: {"session_id": sid} for jid, sid in zip(job_ids, session_ids)},
            ),
        )

    async def test_cleanup_waits_for_every_child_kernel(self, manager):
        gates = [asyncio.Event(), asyncio.Event()]
        session_ids = [
            manager.create_session(data={}, user_identity="u", user_token="t", token_claims={}) for _ in gates
        ]
        kms = [register_kernel(manager, sid, name=sid, gate=g)[0] for sid, g in zip(session_ids, gates)]

        server = self._fake_server(manager, session_ids)
        cleanup = asyncio.create_task(CodeExecutionServer._cleanup_parallel_batch_sessions(server, "b1"))
        await let_teardown_start()

        assert all(km.shutdown_started for km in kms)
        assert not cleanup.done(), "cleanup reported done while child kernels were still shutting down"

        for gate in gates:
            gate.set()
        await asyncio.wait_for(cleanup, timeout=5)

        assert all(km.shutdown_finished for km in kms)
        assert all(sid not in manager._kernels for sid in session_ids)

    async def test_one_child_failing_does_not_abandon_the_others(self, manager, caplog):
        session_ids = [
            manager.create_session(data={}, user_identity="u", user_token="t", token_claims={}) for _ in range(2)
        ]
        kms = [register_kernel(manager, sid, name=sid)[0] for sid in session_ids]

        async def boom(session_id: str) -> None:
            raise RuntimeError("teardown exploded")

        original = manager.aclose_session

        async def aclose(session_id: str) -> None:
            if session_id == session_ids[0]:
                return await boom(session_id)
            return await original(session_id)

        manager.aclose_session = aclose  # type: ignore[method-assign]

        server = self._fake_server(manager, session_ids)
        with caplog.at_level(logging.DEBUG):
            await CodeExecutionServer._cleanup_parallel_batch_sessions(server, "b1")

        assert kms[1].shutdown_finished, "a failure on one child must not abandon the rest of the batch"
        assert any("Failed to close parallel child session" in r.getMessage() for r in caplog.records)

    async def test_cleanup_runs_only_once_per_batch(self, manager):
        session_ids = [manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})]
        register_kernel(manager, session_ids[0], name="only")

        server = self._fake_server(manager, session_ids)
        await CodeExecutionServer._cleanup_parallel_batch_sessions(server, "b1")
        assert server._parallel_batches["b1"]["cleanup_done"] is True

        # A second pass must be inert rather than re-closing sessions.
        await CodeExecutionServer._cleanup_parallel_batch_sessions(server, "b1")
