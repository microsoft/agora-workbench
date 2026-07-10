"""Tests for execution_mode (sync, async_only, adaptive) on ServerConfig and CodeExecutionServer."""

import asyncio
import json

import pytest
from pydantic import ValidationError

from ..code_execution_models import ServerConfig
from ..sessions import set_current_session


# ============================================================================
# ServerConfig field validation
# ============================================================================


class TestExecutionModeConfig:
    """Validate the execution_mode and promotion_threshold_s fields on ServerConfig."""

    def test_default_execution_mode_is_sync(self):
        config = ServerConfig(name="t", description="d", type="uv", dependency_file="numpy\n")
        assert config.execution_mode == "sync"

    def test_default_promotion_threshold(self):
        config = ServerConfig(name="t", description="d", type="uv", dependency_file="numpy\n")
        assert config.promotion_threshold_s == 30.0

    def test_async_only_mode_accepted(self):
        config = ServerConfig(
            name="t", description="d", type="uv", dependency_file="numpy\n", execution_mode="async_only"
        )
        assert config.execution_mode == "async_only"

    def test_adaptive_mode_accepted(self):
        config = ServerConfig(
            name="t", description="d", type="uv", dependency_file="numpy\n", execution_mode="adaptive"
        )
        assert config.execution_mode == "adaptive"

    def test_invalid_execution_mode_rejected(self):
        with pytest.raises(ValidationError):
            ServerConfig(
                name="t", description="d", type="uv", dependency_file="numpy\n", execution_mode="invalid"
            )

    def test_promotion_threshold_must_be_positive(self):
        with pytest.raises(ValidationError):
            ServerConfig(
                name="t",
                description="d",
                type="uv",
                dependency_file="numpy\n",
                execution_mode="adaptive",
                promotion_threshold_s=0,
            )

    def test_promotion_threshold_negative_rejected(self):
        with pytest.raises(ValidationError):
            ServerConfig(
                name="t",
                description="d",
                type="uv",
                dependency_file="numpy\n",
                promotion_threshold_s=-5,
            )

    def test_custom_promotion_threshold(self):
        config = ServerConfig(
            name="t",
            description="d",
            type="uv",
            dependency_file="numpy\n",
            execution_mode="adaptive",
            promotion_threshold_s=120.0,
        )
        assert config.promotion_threshold_s == 120.0

    def test_serialization_includes_execution_mode(self):
        config = ServerConfig(
            name="t", description="d", type="uv", dependency_file="numpy\n", execution_mode="adaptive"
        )
        data = json.loads(config.model_dump_json())
        assert data["execution_mode"] == "adaptive"
        assert data["promotion_threshold_s"] == 30.0


# ============================================================================
# async_only execution mode
# ============================================================================


@pytest.mark.asyncio
async def test_async_only_always_returns_job_handle(test_server):
    """In async_only mode, every execution returns a job handle instead of inline results."""
    original_mode = test_server.server_config.execution_mode
    test_server.server_config.execution_mode = "async_only"
    try:
        session_id = test_server.session_manager.create_session(
            data={}, user_identity="test_user", user_token="test-token", token_claims={}
        )
        session = test_server.session_manager.get_session(session_id)
        set_current_session(session)
        try:
            job_result = await test_server._execute_code_background("print('hello')", timeout=10)
        finally:
            set_current_session(None)

        assert "job_id" in job_result
        assert job_result["status"] == "running"
        assert job_result["session_id"] == session_id

        # Wait for completion
        for _ in range(30):
            status = test_server.session_manager.check_background_job(job_result["job_id"])
            if status["status"] != "running":
                break
            await asyncio.sleep(0.1)

        assert status["status"] == "completed"
        assert "hello" in status["stdout"]
    finally:
        test_server.server_config.execution_mode = original_mode


# ============================================================================
# adaptive execution mode — fast path (completes within threshold)
# ============================================================================


@pytest.mark.asyncio
async def test_adaptive_fast_execution_returns_tuple(test_server):
    """Adaptive mode returns sync results when code completes within threshold."""
    session_id = test_server.session_manager.create_session(
        data={}, user_identity="test_user", user_token="test-token", token_claims={}
    )

    result = await test_server.session_manager.start_promoted_execution_for_session(
        session_id=session_id,
        code="print('fast result')",
        timeout=10,
        promotion_threshold_s=5.0,
    )

    # Should be a tuple, not a dict (completed within threshold)
    assert isinstance(result, tuple), f"Expected tuple, got {type(result)}: {result}"
    stdout, stderr, success, displays, artifacts = result
    assert success is True
    assert "fast result" in stdout


# ============================================================================
# adaptive execution mode — slow path (promoted to background)
# ============================================================================


@pytest.mark.asyncio
async def test_adaptive_slow_execution_promotes_to_background(test_server):
    """Adaptive mode promotes to background when code exceeds threshold."""
    session_id = test_server.session_manager.create_session(
        data={}, user_identity="test_user", user_token="test-token", token_claims={}
    )

    result = await test_server.session_manager.start_promoted_execution_for_session(
        session_id=session_id,
        code="import time; time.sleep(3); print('slow result')",
        timeout=30,
        promotion_threshold_s=1.0,  # 1 second threshold — sleep(3) will exceed it
    )

    # Should be a dict (promoted to background)
    assert isinstance(result, dict), f"Expected dict, got {type(result)}: {result}"
    assert result["promoted"] is True
    assert "job_id" in result
    assert result["session_id"] == session_id

    # Wait for the background job to complete
    job_id = result["job_id"]
    final_status = None
    for _ in range(50):
        status = test_server.session_manager.check_background_job(job_id)
        if status["status"] != "running":
            final_status = status
            break
        await asyncio.sleep(0.1)

    assert final_status is not None
    assert final_status["status"] == "completed"
    assert final_status["success"] is True
    assert "slow result" in final_status["stdout"]


# ============================================================================
# adaptive mode — session state preserved after promotion
# ============================================================================


@pytest.mark.asyncio
async def test_adaptive_promoted_execution_preserves_session_state(test_server):
    """Variables set during a promoted background execution are accessible in follow-up calls."""
    session_id = test_server.session_manager.create_session(
        data={}, user_identity="test_user", user_token="test-token", token_claims={}
    )

    result = await test_server.session_manager.start_promoted_execution_for_session(
        session_id=session_id,
        code="import time; time.sleep(2); promoted_var = 42; print('promoted done')",
        timeout=30,
        promotion_threshold_s=0.5,
    )

    assert isinstance(result, dict)
    assert result["promoted"] is True

    # Wait for completion
    job_id = result["job_id"]
    for _ in range(50):
        status = test_server.session_manager.check_background_job(job_id)
        if status["status"] != "running":
            break
        await asyncio.sleep(0.1)

    assert status["status"] == "completed"

    # Follow-up sync execution should see the variable
    follow_up = await test_server.execute_code_with_session(
        code="print(promoted_var)", timeout=10, session_id=session_id
    )
    assert follow_up.success is True
    assert "42" in follow_up.stdout
