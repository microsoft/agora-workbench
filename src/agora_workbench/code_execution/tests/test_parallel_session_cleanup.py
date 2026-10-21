import asyncio
import json
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from .. import CodeExecutionServer, ServerConfig
from ..auth import create_noop_auth_config
from ..code_execution_models import CodeExecutionResult


@pytest_asyncio.fixture(loop_scope="function")
async def parallel_server(tmp_path, monkeypatch):
    server = CodeExecutionServer(
        server_config=ServerConfig(
            name="cleanup",
            description="Parallel lifecycle regression tests",
            dependency_file="",
            type="uv",
            auto_build=False,
            build_dir=tmp_path / "build",
        ),
        auth_config=create_noop_auth_config(),
        working_dir=tmp_path,
    )
    started = asyncio.Event()

    async def execute(**kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(server, "_inject_tool_proxies", AsyncMock())
    monkeypatch.setattr(server, "_execute_code_with_tracing", execute)
    yield server, started
    tasks = [job["task"] for job in server._parallel_jobs.values() if job.get("task")]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await server.session_manager.aclose_all_sessions()


def create_parent(server, session_id=None):
    manager = server.session_manager
    sid = manager.create_session(
        data={}, user_identity="test-user", user_token="test-token", token_claims={}, session_id=session_id
    )
    return manager.get_session(sid)


async def start_batch(server, parent):
    return await server._parallel_execute_for_session(
        parent_session=parent,
        code="result = value",
        inputs=[{"value": 1}, {"value": 2}],
        timeout=30,
        result_variable="result",
    )


@pytest.mark.parametrize("close_path", ["async", "sync", "thread", "foreign_loop", "tool"])
async def test_parent_close_cancels_children_and_retires_batches(parallel_server, close_path):
    server, started = parallel_server
    server._parallel_semaphore = asyncio.Semaphore(1)
    parent = create_parent(server, "reused-parent")
    batch = await start_batch(server, parent)
    await started.wait()
    tasks = [server._parallel_jobs[job["job_id"]]["task"] for job in batch["jobs"]]

    if close_path == "tool":
        close = (await server.mcp.get_tool("cleanup_close_session")).fn
        result = json.loads(await close(parent.session_id))
        assert result["success"]
        assert result["remaining_sessions"] == 0
    elif close_path == "sync":
        server.session_manager.close_session(parent.session_id)
        await server.session_manager.await_resource_cleanup()
    elif close_path == "thread":
        await asyncio.wait_for(asyncio.to_thread(server.session_manager.close_session, parent.session_id), timeout=5)
    elif close_path == "foreign_loop":
        await asyncio.wait_for(
            asyncio.to_thread(lambda: asyncio.run(server.session_manager.aclose_session(parent.session_id))), timeout=5
        )
    else:
        await server.session_manager.aclose_session(parent.session_id)

    assert all(task.done() for task in tasks)
    assert server.session_manager.storage.count() == 0
    assert not server._parallel_batches
    assert not server._parallel_jobs
    assert not server._parallel_job_by_session
    create_parent(server, parent.session_id)
    with pytest.raises(ValueError, match="not found"):
        await server._check_batch_payload(batch["batch_id"])


async def test_parent_close_leaves_other_parents_batches_running(parallel_server):
    server, started = parallel_server
    parent = create_parent(server)
    other = create_parent(server)
    first = await start_batch(server, parent)
    second = await start_batch(server, parent)
    unaffected = await start_batch(server, other)
    await started.wait()

    await server.session_manager.aclose_session(parent.session_id)

    assert first["batch_id"] not in server._parallel_batches
    assert second["batch_id"] not in server._parallel_batches
    assert unaffected["batch_id"] in server._parallel_batches
    assert all(not server._parallel_jobs[job["job_id"]]["task"].done() for job in unaffected["jobs"])
    assert server.session_manager.get_session(other.session_id) is other


async def test_parent_close_retires_unpolled_completed_batch(parallel_server, monkeypatch):
    server, _ = parallel_server
    parent = create_parent(server)
    monkeypatch.setattr(server, "_execute_code_with_tracing", AsyncMock(return_value=CodeExecutionResult(success=True)))
    monkeypatch.setattr(server, "_capture_result_variable", AsyncMock(return_value=1))
    batch = await start_batch(server, parent)
    await asyncio.gather(*(server._parallel_jobs[job["job_id"]]["task"] for job in batch["jobs"]))

    await server.session_manager.aclose_session(parent.session_id)

    assert not server._parallel_batches
    assert not server._parallel_jobs
    assert server.session_manager.storage.count() == 0


async def test_cancel_batch_before_children_start_retires_batch(parallel_server):
    server, _ = parallel_server
    parent = create_parent(server)
    batch = await start_batch(server, parent)

    result = await server._cancel_batch_payload(batch["batch_id"])

    assert result["running"] == 0
    assert all(job["status"] == "cancelled" for job in result["jobs"])
    assert not server._parallel_batches
    assert server.session_manager.storage.count() == 1


@pytest.mark.parametrize("replace_parent", [False, True])
async def test_closed_parent_cannot_launch_a_batch(parallel_server, replace_parent):
    server, _ = parallel_server
    parent = create_parent(server)
    await server.session_manager.aclose_session(parent.session_id)
    if replace_parent:
        create_parent(server, parent.session_id)

    with pytest.raises(ValueError, match="not found|closing|replaced"):
        await start_batch(server, parent)

    assert not server._parallel_batches
    assert server.session_manager.storage.count() == int(replace_parent)


async def test_parent_close_joins_concurrent_batch_cleanup(parallel_server, monkeypatch):
    server, started = parallel_server
    parent = create_parent(server)
    batch = await start_batch(server, parent)
    await started.wait()
    child = server.session_manager.get_session(batch["jobs"][0]["session_id"])
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    parent_cleanup_joined = asyncio.Event()
    original_cleanup = server._cleanup_parallel_batch_sessions
    original_close = child.aclose

    async def blocked_close():
        cleanup_started.set()
        await release_cleanup.wait()
        await original_close()

    async def tracked_cleanup(batch_id):
        if cleanup_started.is_set():
            parent_cleanup_joined.set()
        await original_cleanup(batch_id)

    monkeypatch.setattr(child, "aclose", blocked_close)
    monkeypatch.setattr(server, "_cleanup_parallel_batch_sessions", tracked_cleanup)
    cancel = asyncio.create_task(server._cancel_batch_payload(batch["batch_id"]))
    await cleanup_started.wait()
    close = asyncio.create_task(server.session_manager.aclose_session(parent.session_id))
    try:
        await parent_cleanup_joined.wait()
        await asyncio.sleep(0)
        assert not close.done()
        assert batch["batch_id"] in server._parallel_batches
    finally:
        release_cleanup.set()
        results = await asyncio.gather(cancel, close, return_exceptions=True)

    assert results[1] is None
    assert not server._parallel_batches
    assert not server._parallel_jobs
    assert server.session_manager.storage.count() == 0


async def test_cancelled_cleanup_waiter_can_rejoin_retained_teardown(parallel_server, monkeypatch):
    server, started = parallel_server
    parent = create_parent(server)
    batch = await start_batch(server, parent)
    await started.wait()
    child = server.session_manager.get_session(batch["jobs"][0]["session_id"])
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    original_close = child.aclose

    async def blocked_close():
        cleanup_started.set()
        await release_cleanup.wait()
        await original_close()

    monkeypatch.setattr(child, "aclose", blocked_close)
    resources = parent.extensions["_agora_parallel_batches"]
    waiter = asyncio.create_task(resources.aclose())
    await cleanup_started.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    drain = asyncio.create_task(resources.aclose())
    try:
        await asyncio.sleep(0)
        assert not drain.done()
        assert batch["batch_id"] in server._parallel_batches
    finally:
        release_cleanup.set()
        await drain

    assert not server._parallel_batches
    assert not server._parallel_jobs
    assert server.session_manager.storage.count() == 1
