"""Real-kernel tests for detecting kernel processes that die during execution."""

import asyncio
import os
import signal
import time

import pytest

from .conftest import TEST_USER_TOKEN

# Disable core dumps so aborting kernels do not litter the working directory.
# The sleep lets the IOPub thread publish the partial output before the crash.
_ABORT_KERNEL = (
    "import os, resource, time\n"
    "resource.setrlimit(resource.RLIMIT_CORE, (0, 0))\n"
    "print('before crash', flush=True)\n"
    "time.sleep(0.5)\n"
)


def _new_session(server) -> str:
    return server.session_manager.create_session(
        data={}, user_identity="test_user", user_token=TEST_USER_TOKEN, token_claims={}
    )


async def _assert_fresh_kernel_works(manager, session_id: str, previous_generation) -> None:
    stdout, stderr, success, _, _ = await manager.execute_code_for_session(session_id, "print(6 * 7)", timeout=30)
    assert success, stderr
    assert "42" in stdout
    assert manager.get_kernel_generation(session_id) != previous_generation


@pytest.mark.asyncio
async def test_sync_execution_fails_fast_when_kernel_aborts(test_server):
    manager = test_server.session_manager
    session_id = _new_session(test_server)
    try:
        _, _, success, _, _ = await manager.execute_code_for_session(session_id, "x = 1", timeout=30)
        assert success
        generation = manager.get_kernel_generation(session_id)

        started = time.monotonic()
        stdout, stderr, success, _, _ = await manager.execute_code_for_session(
            session_id, _ABORT_KERNEL + "os.abort()", timeout=120
        )

        assert time.monotonic() - started < 20
        assert success is False
        assert "before crash" in stdout
        assert f"Kernel process for session {session_id} exited unexpectedly" in stderr
        assert "SIGABRT" in stderr
        assert session_id not in manager._kernels

        await _assert_fresh_kernel_works(manager, session_id, generation)
        # State from the dead kernel is gone.
        _, stderr, success, _, _ = await manager.execute_code_for_session(session_id, "print(x)", timeout=30)
        assert success is False
        assert "NameError" in stderr
    finally:
        await manager.aclose_session(session_id)


@pytest.mark.asyncio
async def test_background_job_fails_fast_when_kernel_is_killed(test_server):
    manager = test_server.session_manager
    session_id = _new_session(test_server)
    try:
        started = time.monotonic()
        handle = await manager.start_background_execution_for_session(
            session_id, _ABORT_KERNEL + "os.kill(os.getpid(), 9)", timeout=120
        )
        generation = manager.get_kernel_generation(session_id)
        result = await asyncio.wait_for(manager.await_background_job(handle["job_id"]), timeout=30)

        assert time.monotonic() - started < 30
        assert result is not None
        assert result["status"] == "failed"
        assert result["success"] is False
        assert "before crash" in result["stdout"]
        assert "exited unexpectedly" in result["error"]
        assert "SIGKILL" in result["error"]
        assert "artifacts" in result
        assert manager._get_running_job_for_session(session_id) is None

        await _assert_fresh_kernel_works(manager, session_id, generation)
    finally:
        await manager.aclose_session(session_id)


@pytest.mark.asyncio
async def test_adaptive_execution_detects_kernel_death_before_and_after_promotion(test_server):
    manager = test_server.session_manager
    session_id = _new_session(test_server)
    try:
        # Dies before the promotion threshold: foreground failure.
        _, _, success, _, _ = await manager.execute_code_for_session(session_id, "pass", timeout=30)
        assert success
        generation = manager.get_kernel_generation(session_id)
        result = await manager.start_promoted_execution_for_session(
            session_id, _ABORT_KERNEL + "os.abort()", timeout=120, promotion_threshold_s=60
        )
        assert isinstance(result, tuple)
        stdout, stderr, success, _, _ = result
        assert success is False
        assert "before crash" in stdout
        assert "exited unexpectedly" in stderr

        await _assert_fresh_kernel_works(manager, session_id, generation)
        generation = manager.get_kernel_generation(session_id)

        # Dies after promotion: the background job fails.
        result = await manager.start_promoted_execution_for_session(
            session_id,
            _ABORT_KERNEL + "time.sleep(1.5)\nos.abort()",
            timeout=120,
            promotion_threshold_s=0.5,
        )
        assert isinstance(result, dict) and result["promoted"] is True
        job = await asyncio.wait_for(manager.await_background_job(result["job_id"]), timeout=30)
        assert job is not None
        assert job["status"] == "failed"
        assert "exited unexpectedly" in job["error"]

        await _assert_fresh_kernel_works(manager, session_id, generation)
    finally:
        await manager.aclose_session(session_id)


@pytest.mark.asyncio
async def test_dead_idle_kernel_is_replaced_on_next_call(test_server):
    manager = test_server.session_manager
    session_id = _new_session(test_server)
    try:
        stdout, _, success, _, _ = await manager.execute_code_for_session(
            session_id, "import os\nprint(os.getpid())", timeout=30
        )
        assert success
        generation = manager.get_kernel_generation(session_id)
        os.kill(int(stdout.strip()), signal.SIGKILL)
        km, _ = manager._kernels[session_id]
        for _ in range(50):
            if not await km.is_alive():
                break
            await asyncio.sleep(0.1)

        await _assert_fresh_kernel_works(manager, session_id, generation)
    finally:
        await manager.aclose_session(session_id)
