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
import time
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest

from ..server import CodeExecutionServer
from ..sessions.manager import KERNEL_BOOTSTRAP_TOOL_PROXIES, SessionManager, _BackgroundJob


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
    return km, kc


async def let_teardown_start():
    """Yield enough for a scheduled teardown to reach its first await."""
    for _ in range(3):
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
        assert "s1" not in manager._kernel_execute_locks
        assert manager.get_kernel_generation("s1") is None

        gate.set()
        await task

    async def test_second_teardown_claims_nothing(self, manager):
        gate = asyncio.Event()
        km_first, _ = register_kernel(manager, "s1", name="FIRST", gate=gate)

        first = asyncio.create_task(manager._shutdown_kernel("s1"))
        await let_teardown_start()

        # A second teardown for the same session finds nothing to claim.
        await manager._shutdown_kernel("s1")

        gate.set()
        await first
        assert km_first.shutdown_finished

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
        await stale

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
        await stale

        assert (outputs / "result.csv").exists()


# ---------------------------------------------------------------------------
# Teardowns coalesce onto one referenced task
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCoalescing:
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
                await task
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
        await first

    async def test_task_is_referenced_while_running_and_released_after(self, manager):
        gate = asyncio.Event()
        register_kernel(manager, "s1", gate=gate)

        task = manager._schedule_kernel_shutdown("s1")
        await let_teardown_start()
        assert manager._kernel_shutdown_tasks.get("s1") is task, "an unreferenced task can be GC-ed mid-flight"

        gate.set()
        await task
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
        async def failing(session_id):
            raise RuntimeError("boom")

        manager._shutdown_kernel = failing
        with caplog.at_level(logging.ERROR):
            task = manager._schedule_kernel_shutdown("s1")
            assert task is not None
            with pytest.raises(RuntimeError):
                await task

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
        await task
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
            await waiter

        gate.set()
        await task
        assert km.shutdown_finished is True


# ---------------------------------------------------------------------------
# A replacement kernel is not built alongside a dying one
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestKernelRebuildWaits:
    async def test_get_or_create_waits_for_pending_teardown(self, manager, monkeypatch):
        from .. import sessions as sessions_pkg

        gate = asyncio.Event()
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
        await teardown
        await create

        assert created_while_old_alive == [True], "replacement was built before the old kernel finished shutting down"
        assert "s1" in manager._kernels


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

    def test_no_warning_when_there_is_simply_no_kernel(self, manager, caplog):
        """The benign ``None`` (nothing to tear down) must stay quiet, or the
        warning becomes noise operators learn to ignore."""
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})

        with caplog.at_level(logging.WARNING):
            manager.close_session(session_id)

        assert not [r for r in caplog.records if "no running event loop" in r.getMessage()]


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
