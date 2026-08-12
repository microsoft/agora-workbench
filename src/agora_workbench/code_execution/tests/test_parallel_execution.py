import asyncio
import json

import pytest

from ..sessions import (
    set_current_request_token,
    set_current_token_claims,
    set_current_user_identity,
)

MAX_POLL_ATTEMPTS = 80

# Identity used by tests that exercise the authenticated MCP tool path.
TOOL_USER_IDENTITY = "test-user-oid@test-tenant-id"
TOOL_USER_TOKEN = "test-user-token"
TOOL_TOKEN_CLAIMS = {"oid": "test-user-oid", "tid": "test-tenant-id"}


def _set_tool_auth_context() -> None:
    """Populate the request-scoped auth context the batch tools verify against."""
    set_current_user_identity(TOOL_USER_IDENTITY)
    set_current_request_token(TOOL_USER_TOKEN)
    set_current_token_claims(TOOL_TOKEN_CLAIMS)


def _clear_tool_auth_context() -> None:
    set_current_user_identity(None)
    set_current_request_token(None)
    set_current_token_claims(None)


@pytest.mark.asyncio
async def test_inspect_session_payload_reports_namespace(test_server):
    session_id = test_server.session_manager.create_session(
        data={},
        user_identity="test_user",
        user_token="test-token",
        token_claims={},
    )
    try:
        result = await test_server.execute_code_with_session("value = {'ok': True}", timeout=10, session_id=session_id)
        assert result.success

        payload = await test_server._inspect_session_payload(session_id)
        assert payload["session_id"] == session_id
        assert payload["status"] == "idle"
        assert "value" in payload["namespace"]
        assert payload["namespace"]["value"]["type"] == "dict"
        assert "ok" in payload["namespace"]["value"]["repr"]
    finally:
        test_server.session_manager.close_session(session_id)


@pytest.mark.asyncio
async def test_parallel_execute_for_session_returns_results(test_server):
    parent_session_id = test_server.session_manager.create_session(
        data={},
        user_identity="test_user",
        user_token="test-token",
        token_claims={},
    )
    parent = test_server.session_manager.get_session(parent_session_id)

    try:
        payload = await test_server._parallel_execute_for_session(
            parent_session=parent,
            code="result = {'double': value * 2}",
            inputs=[{"value": 2}, {"value": 3}],
            timeout=20,
            result_variable="result",
        )

        batch_id = payload["batch_id"]
        for _ in range(MAX_POLL_ATTEMPTS):
            status = await test_server._check_batch_payload(batch_id)
            if status["status"] != "running":
                break
            await asyncio.sleep(0.1)

        assert status["status"] == "completed"
        assert status["completed"] == 2
        assert status["failed"] == 0

        results_by_input = {job["input_index"]: job.get("result") for job in status["jobs"]}
        assert results_by_input[0]["double"] == 4
        assert results_by_input[1]["double"] == 6

        for job in status["jobs"]:
            with pytest.raises(ValueError):
                test_server.session_manager.get_session(job["session_id"])
    finally:
        test_server.session_manager.close_session(parent_session_id)


@pytest.mark.asyncio
async def test_parallel_execute_cancellation(test_server):
    """Cancel a running batch and assert all jobs are cancelled and sessions closed."""
    parent_session_id = test_server.session_manager.create_session(
        data={},
        user_identity="test_user",
        user_token="test-token",
        token_claims={},
    )
    parent = test_server.session_manager.get_session(parent_session_id)

    try:
        payload = await test_server._parallel_execute_for_session(
            parent_session=parent,
            code="import time; time.sleep(30)",
            inputs=[{"x": 1}, {"x": 2}],
            timeout=60,
            result_variable="",
        )
        batch_id = payload["batch_id"]
        job_session_ids = [job["session_id"] for job in payload["jobs"]]

        # Give tasks a moment to start before cancelling.
        await asyncio.sleep(0.5)

        cancel_result = await test_server._cancel_batch_payload(batch_id)

        assert cancel_result["running"] == 0
        for job in cancel_result["jobs"]:
            assert job["status"] in {"cancelled", "failed"}

        # All child sessions should be closed after cancellation.
        for session_id in job_session_ids:
            with pytest.raises(ValueError):
                test_server.session_manager.get_session(session_id)
    finally:
        test_server.session_manager.close_session(parent_session_id)


@pytest.mark.asyncio
async def test_parallel_execute_job_failure_partial_failure(test_server):
    """A failing job in a batch produces a partial_failure aggregate status."""
    parent_session_id = test_server.session_manager.create_session(
        data={},
        user_identity="test_user",
        user_token="test-token",
        token_claims={},
    )
    parent = test_server.session_manager.get_session(parent_session_id)

    try:
        payload = await test_server._parallel_execute_for_session(
            parent_session=parent,
            code=("if value == 0:\n    raise RuntimeError('intentional failure')\nresult = value * 2"),
            inputs=[{"value": 0}, {"value": 3}],
            timeout=20,
            result_variable="result",
        )

        batch_id = payload["batch_id"]
        for _ in range(MAX_POLL_ATTEMPTS):
            status = await test_server._check_batch_payload(batch_id)
            if status["status"] != "running":
                break
            await asyncio.sleep(0.1)

        assert status["status"] == "partial_failure"
        assert status["failed"] == 1
        assert status["completed"] == 1

        # Child sessions should be cleaned up after the batch finishes.
        for job in status["jobs"]:
            with pytest.raises(ValueError):
                test_server.session_manager.get_session(job["session_id"])
    finally:
        test_server.session_manager.close_session(parent_session_id)


@pytest.mark.asyncio
async def test_parallel_execute_invalid_input_key_leaves_no_orphans(test_server):
    """An invalid input key should raise before creating any sessions or batch state."""
    parent_session_id = test_server.session_manager.create_session(
        data={},
        user_identity="test_user",
        user_token="test-token",
        token_claims={},
    )
    parent = test_server.session_manager.get_session(parent_session_id)
    sessions_before = {s["session_id"] for s in test_server.session_manager.list_sessions()}
    batches_before = set(test_server._parallel_batches.keys())

    try:
        with pytest.raises(ValueError, match="inputs\\[1\\]"):
            await test_server._parallel_execute_for_session(
                parent_session=parent,
                code="result = x",
                inputs=[{"x": 1}, {"not-an-identifier": 2}],
                timeout=10,
                result_variable="result",
            )

        # No new sessions or batch state should have been created.
        sessions_after = {s["session_id"] for s in test_server.session_manager.list_sessions()}
        assert sessions_after == sessions_before
        assert set(test_server._parallel_batches.keys()) == batches_before
    finally:
        test_server.session_manager.close_session(parent_session_id)


@pytest.mark.asyncio
async def test_check_batch_tool_returns_results(test_server):
    """The registered check_batch MCP tool must return batch results.

    Regression test: the tool resolves the owning session from the
    ``_check_batch_payload`` result, so omitting ``parent_session_id`` from that
    payload made every call fail with a spurious 404. The other tests in this
    module call ``_check_batch_payload`` directly and cannot catch that.
    """
    _set_tool_auth_context()
    parent_session_id = test_server.session_manager.create_session(
        data={},
        user_identity=TOOL_USER_IDENTITY,
        user_token=TOOL_USER_TOKEN,
        token_claims=TOOL_TOKEN_CLAIMS,
    )
    parent = test_server.session_manager.get_session(parent_session_id)
    check_batch = (await test_server.mcp.get_tool("test_check_batch")).fn

    try:
        payload = await test_server._parallel_execute_for_session(
            parent_session=parent,
            code="result = {'double': value * 2}",
            inputs=[{"value": 2}, {"value": 3}],
            timeout=20,
            result_variable="result",
        )
        batch_id = payload["batch_id"]

        for _ in range(MAX_POLL_ATTEMPTS):
            _set_tool_auth_context()
            status = json.loads(await check_batch(None, batch_id))
            assert status.get("success") is not False, f"check_batch failed: {status.get('error')}"
            if status["status"] != "running":
                break
            await asyncio.sleep(0.1)

        assert status["status"] == "completed"
        assert status["completed"] == 2
        assert status["failed"] == 0
        assert status["parent_session_id"] == parent_session_id

        results_by_input = {job["input_index"]: job.get("result") for job in status["jobs"]}
        assert results_by_input[0]["double"] == 4
        assert results_by_input[1]["double"] == 6
    finally:
        _clear_tool_auth_context()
        test_server.session_manager.close_session(parent_session_id)


@pytest.mark.asyncio
async def test_cancel_batch_tool_cancels_running_batch(test_server):
    """The registered cancel_batch MCP tool must cancel a running batch."""
    _set_tool_auth_context()
    parent_session_id = test_server.session_manager.create_session(
        data={},
        user_identity=TOOL_USER_IDENTITY,
        user_token=TOOL_USER_TOKEN,
        token_claims=TOOL_TOKEN_CLAIMS,
    )
    parent = test_server.session_manager.get_session(parent_session_id)
    cancel_batch = (await test_server.mcp.get_tool("test_cancel_batch")).fn

    try:
        payload = await test_server._parallel_execute_for_session(
            parent_session=parent,
            code="import time\ntime.sleep(30)\nresult = value",
            inputs=[{"value": 1}, {"value": 2}],
            timeout=60,
            result_variable="result",
        )

        # Give tasks a moment to start before cancelling.
        await asyncio.sleep(0.5)

        _set_tool_auth_context()
        status = json.loads(await cancel_batch(None, payload["batch_id"]))

        assert status.get("success") is not False, f"cancel_batch failed: {status.get('error')}"
        assert status["running"] == 0
        for job in status["jobs"]:
            assert job["status"] in {"cancelled", "failed"}
    finally:
        _clear_tool_auth_context()
        test_server.session_manager.close_session(parent_session_id)


@pytest.mark.asyncio
async def test_check_batch_tool_rejects_other_users_without_discarding_results(test_server):
    """An unauthorized check_batch call must not consume the owner's batch.

    Building a status payload retires a terminal batch (closing child sessions
    and pruning the registries), so authorization has to be settled before the
    payload is built. Otherwise anyone reaching the tool with a batch id could
    destroy results they are not allowed to read.
    """
    _set_tool_auth_context()
    parent_session_id = test_server.session_manager.create_session(
        data={},
        user_identity=TOOL_USER_IDENTITY,
        user_token=TOOL_USER_TOKEN,
        token_claims=TOOL_TOKEN_CLAIMS,
    )
    parent = test_server.session_manager.get_session(parent_session_id)
    check_batch = (await test_server.mcp.get_tool("test_check_batch")).fn

    try:
        payload = await test_server._parallel_execute_for_session(
            parent_session=parent,
            code="result = {'double': value * 2}",
            inputs=[{"value": 2}, {"value": 3}],
            timeout=20,
            result_variable="result",
        )
        batch_id = payload["batch_id"]
        job_ids = [job["job_id"] for job in payload["jobs"]]

        # Wait for the jobs to finish by reading job state directly: polling via
        # _check_batch_payload would itself retire the batch.
        for _ in range(MAX_POLL_ATTEMPTS):
            if all(test_server._parallel_jobs[job_id]["status"] != "running" for job_id in job_ids):
                break
            await asyncio.sleep(0.1)

        # A caller from a different tenant/user must be refused.
        set_current_user_identity("other-user-oid@other-tenant-id")
        set_current_request_token("other-user-token")
        set_current_token_claims({"oid": "other-user-oid", "tid": "other-tenant-id"})

        denied = json.loads(await check_batch(None, batch_id))
        assert denied.get("success") is False
        assert "Not authorized" in denied["error"]

        # The rightful owner can still retrieve the results.
        _set_tool_auth_context()
        status = json.loads(await check_batch(None, batch_id))

        assert status.get("success") is not False, f"owner lost access to the batch: {status.get('error')}"
        assert status["status"] == "completed"
        assert status["completed"] == 2
    finally:
        _clear_tool_auth_context()
        test_server.session_manager.close_session(parent_session_id)
