import asyncio
import contextlib
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from .. import CodeExecutionServer, ServerConfig
from .. import batch_exports
from ..auth import create_noop_auth_config
from ..code_execution_models import CodeExecutionResult
from ..sessions import (
    get_current_session,
    set_current_request_token,
    set_current_token_claims,
    set_current_user_identity,
)


@pytest_asyncio.fixture(loop_scope="function")
async def export_server(tmp_path, monkeypatch):
    server = CodeExecutionServer(
        server_config=ServerConfig(
            name="exports",
            description="Batch export tests",
            type="uv",
            dependency_file="",
            auto_build=False,
            build_dir=tmp_path / "build",
        ),
        auth_config=create_noop_auth_config(),
        working_dir=tmp_path,
    )
    namespaces = {}

    async def execute_in_namespace(session_id, code, **_kwargs):
        namespace = namespaces.setdefault(session_id, {})
        stdout = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout):
                exec(code, namespace)
        except Exception as exc:
            return stdout.getvalue(), str(exc), False, [], []
        return stdout.getvalue(), "", True, [], []

    async def execute_batch(code, timeout):
        stdout, stderr, success, _, _ = await execute_in_namespace(get_current_session().session_id, code)
        return CodeExecutionResult(success=success, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(server, "_inject_tool_proxies", AsyncMock())
    monkeypatch.setattr(server, "_execute_code_with_tracing", execute_batch)
    monkeypatch.setattr(server.session_manager, "execute_code_for_session", execute_in_namespace)
    parent_id = server.session_manager.create_session(
        data={},
        user_identity="test-user@test-tenant",
        user_token="test-token",
        token_claims={"oid": "test-user", "tid": "test-tenant"},
    )
    parent = server.session_manager.get_session(parent_id)
    yield server, parent, namespaces
    await server.session_manager.aclose_all_sessions()
    server._clear_auth_context()


def authenticate(parent, other_user=False):
    set_current_user_identity("other-user@test-tenant" if other_user else parent.user_identity)
    set_current_request_token(parent.user_token)
    set_current_token_claims({"oid": "other-user", "tid": "test-tenant"} if other_user else parent.token_claims)


async def finish_batch(server, parent, code="candidate = {value, value + 1}\nresult = value", count=3):
    launched = await server._parallel_execute_for_session(
        parent_session=parent,
        code=code,
        inputs=[{"value": value} for value in range(count)],
        timeout=30,
        result_variable="result",
        export_variable="candidate",
    )
    await asyncio.gather(*(server._parallel_jobs[job["job_id"]]["task"] for job in launched["jobs"]))
    return launched


async def test_poll_then_select_collect_and_retry_without_reimport(export_server):
    server, parent, namespaces = export_server
    launched = await finish_batch(server, parent)
    batch_id = launched["batch_id"]
    stages = list(server._parallel_batches[batch_id]["stages"].values())
    paths = [stage.path for stage in stages]

    first = await server._check_batch_payload(batch_id)
    second = await server._check_batch_payload(batch_id)
    assert first == second
    assert first["status"] == "completed"
    assert first["collection"]["staged_indices"] == [0, 1, 2]
    assert first["collection"]["expires_at"] is not None
    assert server.session_manager.storage.count() == 1
    assert all(path.exists() for path in paths)

    collected = await server._collect_batch_payload(batch_id, "candidates", indices=[2, 0])
    assert collected["success"]
    assert collected["execution_session_id"] == parent.session_id
    assert collected["imported_indices"] == [0, 2]
    assert namespaces[parent.session_id]["candidates"] == {0: {0, 1}, 2: {2, 3}}
    assert collected["skipped"][0]["input_index"] == 1
    assert all(not path.exists() for path in paths)
    assert server._parallel_export_bytes == 0
    assert not server._parallel_jobs

    namespaces[parent.session_id]["candidates"] = "changed after collection"
    assert await server._collect_batch_payload(batch_id, "candidates", indices=[0, 2]) == collected
    assert namespaces[parent.session_id]["candidates"] == "changed after collection"
    assert await server._check_batch_payload(batch_id) == collected
    conflict = await server._collect_batch_payload(batch_id, "another_name")
    assert conflict["reason"] == "already_collected"
    await server._cancel_batch_payload(batch_id)
    assert batch_id not in server._parallel_batches


async def test_export_status_is_independent_and_none_is_a_valid_snapshot(export_server):
    server, parent, namespaces = export_server
    launched = await finish_batch(
        server,
        parent,
        code=(
            "result = value\n"
            "if value == 0:\n"
            "    candidate = None\n"
            "elif value == 1:\n"
            "    raise RuntimeError('execution failure')\n"
            "elif value == 2:\n"
            "    candidate = (i for i in range(2))\n"
        ),
        count=4,
    )
    batch_id = launched["batch_id"]
    status = await server._check_batch_payload(batch_id)
    assert status["status"] == "partial_failure"
    assert [job["status"] for job in status["jobs"]] == ["completed", "failed", "completed", "completed"]
    assert [job["export"]["status"] for job in status["jobs"]] == ["staged", "skipped", "failed", "missing"]

    unavailable = await server._collect_batch_payload(batch_id, "candidates", indices=[0, 2])
    assert unavailable["reason"] == "exports_unavailable"
    assert batch_id in server._parallel_batches
    result = await server._collect_batch_payload(batch_id, "candidates")
    assert result["imported_indices"] == [0]
    assert len(result["skipped"]) == 3
    assert namespaces[parent.session_id]["candidates"] == {0: None}


@pytest.mark.parametrize("limit", ["MAX_TRANSFER_SIZE_BYTES", "MAX_BATCH_EXPORT_BYTES", "MAX_STAGED_EXPORT_BYTES"])
async def test_export_limits_do_not_fail_successful_execution(export_server, monkeypatch, limit):
    server, parent, _ = export_server
    monkeypatch.setattr(batch_exports, limit, 32)
    launched = await finish_batch(server, parent, code="candidate = b'x' * 200\nresult = value", count=1)
    batch_id = launched["batch_id"]
    status = await server._check_batch_payload(batch_id)
    assert status["status"] == "completed"
    assert status["jobs"][0]["export"]["status"] == "too_large"
    assert not server._parallel_batches[batch_id]["stages"]
    assert server._parallel_export_bytes == 0
    result = await server._collect_batch_payload(batch_id, "candidates")
    assert result["reason"] == "exports_unavailable"
    await server._cancel_batch_payload(batch_id)


async def test_shared_object_budget_is_released_on_discard(export_server, monkeypatch):
    server, parent, _ = export_server
    monkeypatch.setattr(batch_exports, "MAX_STAGED_EXPORTS", 1)
    first = await finish_batch(server, parent, count=1)
    second = await finish_batch(server, parent, count=1)
    result = await server._check_batch_payload(second["batch_id"])
    assert result["jobs"][0]["export"]["status"] == "too_large"
    await server._cancel_batch_payload(first["batch_id"])
    third = await finish_batch(server, parent, count=1)
    result = await server._check_batch_payload(third["batch_id"])
    assert result["jobs"][0]["export"]["status"] == "staged"


async def test_failed_stage_cleanup_does_not_change_execution_status_or_leak_budget(export_server, monkeypatch):
    server, parent, _ = export_server
    close = batch_exports.ExportStage.close

    def failing_close(stage):
        close(stage)
        raise OSError("cleanup failed")

    monkeypatch.setattr(batch_exports.ExportStage, "close", failing_close)
    launched = await finish_batch(server, parent, code="result = value", count=1)
    status = await server._check_batch_payload(launched["batch_id"])
    assert status["status"] == "completed"
    assert status["jobs"][0]["export"]["status"] == "missing"
    assert server._parallel_export_bytes == 0
    assert not server._parallel_batches[launched["batch_id"]]["stages"]


def test_staging_allocation_failure_closes_partial_file(tmp_path, monkeypatch):
    directory = tmp_path / "stage"
    directory.mkdir()
    monkeypatch.setattr(batch_exports.tempfile, "mkdtemp", lambda **_: str(directory))

    def failing_fstat(_):
        raise OSError("fstat failed")

    monkeypatch.setattr(batch_exports.os, "fstat", failing_fstat)
    with pytest.raises(OSError, match="fstat failed"):
        batch_exports.ExportStage()
    assert not directory.exists()


async def test_busy_parent_can_be_polled_and_retried_without_losing_exports(export_server, monkeypatch):
    server, parent, namespaces = export_server
    launched = await finish_batch(server, parent, count=1)
    batch_id = launched["batch_id"]
    monkeypatch.setattr(server.session_manager, "_get_running_job_for_session", lambda _: "busy-job")
    busy = await server._collect_batch_payload(batch_id, "candidates")
    assert busy["reason"] == "parent_busy"
    assert busy["retryable"]
    assert busy["job_id"] == "busy-job"
    assert busy["poll_tool"] == "exports_check_job"
    assert (await server._check_batch_payload(batch_id))["collection"]["staged_indices"] == [0]

    monkeypatch.setattr(server.session_manager, "_get_running_job_for_session", lambda _: None)
    result = await server._collect_batch_payload(batch_id, "candidates")
    assert result["success"]
    assert namespaces[parent.session_id]["candidates"] == {0: {0, 1}}


async def test_parent_becoming_busy_during_admission_returns_poll_guidance(export_server, monkeypatch):
    server, parent, _ = export_server
    launched = await finish_batch(server, parent, count=1)
    original = server.session_manager.execute_code_for_session
    busy_job = None
    monkeypatch.setattr(server.session_manager, "_get_running_job_for_session", lambda _: busy_job)

    async def busy_on_admission(**kwargs):
        nonlocal busy_job
        if kwargs["session_id"] == parent.session_id:
            busy_job = "new-background-job"
            return "", f"Session busy - job {busy_job} is still running", False, [], []
        return await original(**kwargs)

    monkeypatch.setattr(server.session_manager, "execute_code_for_session", busy_on_admission)
    busy = await server._collect_batch_payload(launched["batch_id"], "candidates")
    assert busy["reason"] == "parent_busy"
    assert busy["job_id"] == "new-background-job"
    assert busy["poll_tool"] == "exports_check_job"


async def test_parent_closed_during_admission_is_not_retryable(export_server, monkeypatch):
    server, parent, _ = export_server
    launched = await finish_batch(server, parent, count=1)
    original = server.session_manager.execute_code_for_session

    async def close_on_admission(**kwargs):
        if kwargs["session_id"] == parent.session_id:
            server.session_manager.close_session(parent.session_id)
            raise ValueError("Parent is closing")
        return await original(**kwargs)

    monkeypatch.setattr(server.session_manager, "execute_code_for_session", close_on_admission)
    result = await server._collect_batch_payload(launched["batch_id"], "candidates")
    assert result["reason"] == "parent_unavailable"
    assert not result["retryable"]
    await server.session_manager.aclose_session(parent.session_id)
    assert not server._parallel_batches


@pytest.mark.parametrize("overwrite", [False, True])
async def test_destination_conflict_requires_explicit_overwrite(export_server, overwrite):
    server, parent, namespaces = export_server
    launched = await finish_batch(server, parent, count=1)
    namespaces[parent.session_id] = {"candidates": "keep"}
    result = await server._collect_batch_payload(launched["batch_id"], "candidates", overwrite=overwrite)
    if overwrite:
        assert result["success"]
        assert namespaces[parent.session_id]["candidates"] == {0: {0, 1}}
    else:
        assert result["reason"] == "destination_exists"
        assert namespaces[parent.session_id]["candidates"] == "keep"
        assert server._parallel_batches[launched["batch_id"]]["stages"]
        assert (await server._collect_batch_payload(launched["batch_id"], "new_candidates"))["success"]


async def test_load_failure_preserves_destination_and_can_exclude_bad_index(export_server):
    server, parent, namespaces = export_server
    launched = await finish_batch(server, parent, count=2)
    batch_id = launched["batch_id"]
    server._parallel_batches[batch_id]["stages"][1].path.write_bytes(b"not a pickle")
    result = await server._collect_batch_payload(batch_id, "candidates")
    assert result["reason"] == "load_failed"
    assert result["input_index"] == 1
    assert "candidates" not in namespaces[parent.session_id]
    assert server._parallel_batches[batch_id]["stages"]
    result = await server._collect_batch_payload(batch_id, "candidates", indices=[0])
    assert result["success"]
    assert namespaces[parent.session_id]["candidates"] == {0: {0, 1}}


async def test_expiry_discards_even_unpolled_exports_and_kernels(export_server, monkeypatch):
    server, parent, _ = export_server
    monkeypatch.setattr(batch_exports, "EXPORT_RETENTION_SECONDS", 0.01)
    launched = await finish_batch(server, parent, count=1)
    batch = server._parallel_batches[launched["batch_id"]]
    paths = [stage.path for stage in batch["stages"].values()]
    await asyncio.wait_for(batch["expiry_task"], timeout=5)
    assert launched["batch_id"] not in server._parallel_batches
    assert server.session_manager.storage.count() == 1
    assert all(not path.exists() for path in paths)
    assert server._parallel_export_bytes == 0


@pytest.mark.parametrize("polled", [False, True])
async def test_parent_close_discards_staged_exports(export_server, polled):
    server, parent, _ = export_server
    launched = await finish_batch(server, parent, count=1)
    paths = [stage.path for stage in server._parallel_batches[launched["batch_id"]]["stages"].values()]
    if polled:
        await server._check_batch_payload(launched["batch_id"])
    await server.session_manager.aclose_session(parent.session_id)
    assert not server._parallel_batches
    assert not server._parallel_jobs
    assert server._parallel_export_bytes == 0
    assert all(not path.exists() for path in paths)


async def test_cancel_during_staging_releases_reserved_budget(export_server, monkeypatch):
    server, parent, _ = export_server
    started = asyncio.Event()
    original = server.session_manager.execute_code_for_session

    async def blocked_stage(**kwargs):
        if batch_exports.RESULT_MARKER in kwargs["code"]:
            started.set()
            await asyncio.Event().wait()
        return await original(**kwargs)

    monkeypatch.setattr(server.session_manager, "execute_code_for_session", blocked_stage)
    launched = await server._parallel_execute_for_session(
        parent_session=parent,
        code="candidate = value",
        inputs=[{"value": 1}],
        timeout=30,
        result_variable="",
        export_variable="candidate",
    )
    await started.wait()
    stage = next(iter(server._parallel_batches[launched["batch_id"]]["stages"].values()))
    assert server._parallel_export_bytes > 0
    await server._cancel_batch_payload(launched["batch_id"])
    assert not stage.path.exists()
    assert server._parallel_export_bytes == 0
    assert not server._parallel_batches


async def test_cancelled_caller_and_concurrent_polls_do_not_double_collect(export_server, monkeypatch):
    server, parent, namespaces = export_server
    launched = await finish_batch(server, parent, count=1)
    batch_id = launched["batch_id"]
    started, release = asyncio.Event(), asyncio.Event()
    original = server.session_manager.execute_code_for_session
    parent_loads = 0

    async def blocked_load(**kwargs):
        nonlocal parent_loads
        if kwargs["session_id"] == parent.session_id:
            parent_loads += 1
            started.set()
            await release.wait()
        return await original(**kwargs)

    monkeypatch.setattr(server.session_manager, "execute_code_for_session", blocked_load)
    first = asyncio.create_task(server._collect_batch_payload(batch_id, "candidates"))
    await started.wait()
    retry = asyncio.create_task(server._collect_batch_payload(batch_id, "candidates"))
    poll = asyncio.create_task(server._check_batch_payload(batch_id))
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not poll.done()
    release.set()
    result, polled = await asyncio.gather(retry, poll)
    assert result["success"]
    assert polled == result
    assert parent_loads == 1
    assert namespaces[parent.session_id]["candidates"] == {0: {0, 1}}


@pytest.mark.parametrize("cleanup", ["cancel", "parent_close"])
async def test_collection_coordinates_with_teardown(export_server, monkeypatch, cleanup):
    server, parent, _ = export_server
    launched = await finish_batch(server, parent, count=1)
    batch_id = launched["batch_id"]
    started, release = asyncio.Event(), asyncio.Event()
    original = server.session_manager.execute_code_for_session

    async def blocked_load(**kwargs):
        if kwargs["session_id"] == parent.session_id:
            started.set()
            await release.wait()
        return await original(**kwargs)

    monkeypatch.setattr(server.session_manager, "execute_code_for_session", blocked_load)
    collect = asyncio.create_task(server._collect_batch_payload(batch_id, "candidates"))
    await started.wait()
    teardown = asyncio.create_task(
        server._cancel_batch_payload(batch_id)
        if cleanup == "cancel"
        else server.session_manager.aclose_session(parent.session_id)
    )
    await asyncio.sleep(0)
    assert not teardown.done()
    release.set()
    await asyncio.gather(collect, teardown)
    assert batch_id not in server._parallel_batches
    assert server._parallel_export_bytes == 0


async def test_receipts_are_bounded_and_expire(export_server, monkeypatch):
    server, parent, _ = export_server
    monkeypatch.setattr(batch_exports, "MAX_COLLECTION_RECEIPTS", 1)
    first = await finish_batch(server, parent, count=1)
    await server._collect_batch_payload(first["batch_id"], "first")
    second = await finish_batch(server, parent, count=1)
    await server._collect_batch_payload(second["batch_id"], "second")
    assert first["batch_id"] not in server._parallel_batches
    assert second["batch_id"] in server._parallel_batches
    batch = server._parallel_batches[second["batch_id"]]
    server._schedule_parallel_batch_expiry(batch, 0.01)
    await asyncio.wait_for(batch["expiry_task"], timeout=5)
    assert not server._parallel_batches
    assert not parent.extensions["_agora_parallel_batches"].batch_ids


async def test_mcp_launch_targets_explicit_parent_and_checks_collection_ownership(export_server):
    import json

    server, parent, namespaces = export_server
    launch = (await server.mcp.get_tool("exports_parallel_execute")).fn
    collect = (await server.mcp.get_tool("exports_collect_batch")).fn
    authenticate(parent)
    launched = json.loads(
        await launch(
            ctx=SimpleNamespace(session_id="different-transport"),
            code="candidate = {value}\nresult = value",
            inputs=[{"value": 7}],
            execution_session_id=parent.session_id,
            export_variable="candidate",
        )
    )
    assert launched["parent_session_id"] == parent.session_id
    await asyncio.gather(*(server._parallel_jobs[job["job_id"]]["task"] for job in launched["jobs"]))

    authenticate(parent, other_user=True)
    denied = json.loads(await collect(None, launched["batch_id"], "candidates"))
    assert not denied["success"]
    assert "Not authorized" in denied["error"]
    assert server._parallel_batches[launched["batch_id"]]["stages"]

    authenticate(parent)
    result = json.loads(await collect(None, launched["batch_id"], "candidates"))
    assert result["success"]
    assert result["execution_session_id"] == parent.session_id
    assert namespaces[parent.session_id]["candidates"] == {0: {7}}


async def test_pending_batch_limit_rejects_before_creating_children(export_server, monkeypatch):
    server, parent, _ = export_server
    monkeypatch.setattr(batch_exports, "MAX_PENDING_EXPORT_BATCHES", 1)
    launched = await finish_batch(server, parent, count=1)
    sessions_before = server.session_manager.storage.count()
    with pytest.raises(ValueError, match="Collect or cancel"):
        await finish_batch(server, parent, count=1)
    assert server.session_manager.storage.count() == sessions_before
    assert list(server._parallel_batches) == [launched["batch_id"]]


@pytest.mark.parametrize("execution", ["foreground", "adaptive"])
async def test_queued_parent_execution_rechecks_background_busy_inside_lock(export_server, monkeypatch, execution):
    server, parent, _ = export_server
    manager = server.session_manager
    busy_job = None
    monkeypatch.setattr(manager, "_get_running_job_for_session", lambda _: busy_job)
    locked_execute = AsyncMock()
    monkeypatch.setattr(manager, "_execute_code_locked", locked_execute)
    async with manager._kernel_execution_lock(parent.session_id):
        if execution == "foreground":
            operation = manager._execute_code_for_admitted_session(parent.session_id, "pass", 10, None)
        else:
            operation = manager._start_promoted_execution_for_session(parent.session_id, "pass", 10, 1)
        queued = asyncio.create_task(operation)
        await asyncio.sleep(0)
        busy_job = "background-started-while-queued"
    result = await queued
    assert result[2] is False
    assert busy_job in result[1]
    locked_execute.assert_not_awaited()


async def test_background_submission_cannot_race_parent_kernel_loading(export_server, monkeypatch):
    server, parent, _ = export_server
    manager = server.session_manager
    start = AsyncMock(return_value={"job_id": "background"})
    monkeypatch.setattr(manager, "_start_background_execution_for_session", start)
    async with manager._kernel_execution_lock(parent.session_id):
        queued = asyncio.create_task(manager.start_background_execution_for_session(parent.session_id, "pass", 10))
        await asyncio.sleep(0)
        start.assert_not_awaited()
    assert (await queued)["job_id"] == "background"
    start.assert_awaited_once()
