import asyncio

import pytest

MAX_POLL_ATTEMPTS = 80


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
