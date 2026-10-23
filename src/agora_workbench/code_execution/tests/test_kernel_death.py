"""Real-kernel tests for detecting kernel processes that die during execution."""

import asyncio
import json
import os
import signal
import time
from types import SimpleNamespace

import pytest

from .. import code_execution as execution_defaults
from ..sessions import (
    set_current_request_token,
    set_current_session,
    set_current_token_claims,
    set_current_user_identity,
)
from ..sessions import manager as session_manager_module
from ..tool_registry import ToolDefinition, ToolRegistry
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
async def test_dead_background_kernel_is_left_for_collector_to_finalize(test_server, monkeypatch):
    manager = test_server.session_manager
    session_id = _new_session(test_server)
    monkeypatch.setattr(session_manager_module, "_KERNEL_LIVENESS_CHECK_INTERVAL_S", 60.0)
    try:
        code = (
            _ABORT_KERNEL
            + "from pathlib import Path\n"
            + "Path(AGORA_OUTPUT_DIR, 'partial.txt').write_text('partial')\n"
            + "os.kill(os.getpid(), 9)"
        )
        handle = await manager.start_background_execution_for_session(session_id, code, timeout=120)
        generation = manager.get_kernel_generation(session_id)
        km, _ = manager._kernels[session_id]
        for _ in range(50):
            if not await km.is_alive():
                break
            await asyncio.sleep(0.1)

        with pytest.raises(RuntimeError, match=f"Session busy — job {handle['job_id']} is still running"):
            await manager.ensure_kernel_for_session(session_id)

        job = manager._background_jobs[handle["job_id"]]
        assert job.status == "running"
        assert job.task is not None and not job.task.done()
        assert manager.get_kernel_generation(session_id) == generation

        monkeypatch.setattr(session_manager_module, "_KERNEL_LIVENESS_CHECK_INTERVAL_S", 0.0)
        result = await asyncio.wait_for(manager.await_background_job(handle["job_id"]), timeout=30)

        assert result is not None
        assert result["status"] == "failed"
        assert "SIGKILL" in result["error"]
        assert "before crash" in result["stdout"]
        assert [artifact["name"] for artifact in result["artifacts"]] == ["partial.txt"]
        assert session_id not in manager._kernels
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


@pytest.mark.asyncio
async def test_execute_handler_reinjects_tool_helpers_after_dead_idle_kernel(test_server):
    manager = test_server.session_manager
    token_claims = {"oid": "dev-user", "tid": "dev-tenant"}
    session_id = manager.create_session(
        data={},
        user_identity="dev-user@dev-tenant",
        user_token=TEST_USER_TOKEN,
        token_claims=token_claims,
    )
    original_registry = test_server.tool_registry
    registry = ToolRegistry(package="test.tools")
    registry.register_tool(
        ToolDefinition(
            name="recovery_probe",
            description="Probe recovery bootstrap.",
            module="test.tools.recovery",
        )
    )
    test_server.tool_registry = registry
    set_current_user_identity("dev-user@dev-tenant")
    set_current_request_token(TEST_USER_TOKEN)
    set_current_token_claims(token_claims)

    try:
        execute_code_tool = execution_defaults.build_tool(test_server)
        initial = json.loads(
            await execute_code_tool(
                ctx=SimpleNamespace(session_id=None),
                code="import os\nprint(os.getpid())",
                execution_session_id=session_id,
            )
        )
        assert initial["success"] is True
        generation = manager.get_kernel_generation(session_id)
        os.kill(int(initial["stdout"].strip()), signal.SIGKILL)
        km, _ = manager._kernels[session_id]
        for _ in range(50):
            if not await km.is_alive():
                break
            await asyncio.sleep(0.1)

        set_current_user_identity("dev-user@dev-tenant")
        set_current_request_token(TEST_USER_TOKEN)
        set_current_token_claims(token_claims)
        recovered = json.loads(
            await execute_code_tool(
                ctx=SimpleNamespace(session_id=None),
                code="print(callable(list_tools))",
                execution_session_id=session_id,
            )
        )

        assert recovered["success"] is True
        assert recovered["stdout"].strip() == "True"
        assert manager.get_kernel_generation(session_id) != generation
    finally:
        test_server.tool_registry = original_registry
        set_current_session(None)
        set_current_user_identity(None)
        set_current_request_token(None)
        set_current_token_claims(None)
        await manager.aclose_session(session_id)
