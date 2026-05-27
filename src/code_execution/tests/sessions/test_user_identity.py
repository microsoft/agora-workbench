"""Tests for user identity propagation through sessions."""

import asyncio
import queue
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ...code_execution.sessions import Session, SessionConfig, SessionManager


class TestUserIdentityInSessions:
    """Test user identity support in session management."""

    def test_session_with_user_identity(self):
        """Test creating a session with user identity."""
        session = Session(
            session_id="test-123",
            data={"test": "data"},
            session_type="test",
            user_identity="user@example.com",
            user_token="test-token",
            token_claims={},
        )

        assert session.user_identity == "user@example.com"
        assert session.session_id == "test-123"

    def test_session_info_includes_user_identity(self):
        """Test that get_info() includes user identity."""
        session = Session(
            session_id="test-789",
            data={"test": "data"},
            session_type="test",
            user_identity="admin@example.com",
            user_token="test-token",
            token_claims={},
        )

        info = session.get_info()
        assert "user_identity" in info
        assert info["user_identity"] == "admin@example.com"

    def test_session_manager_create_with_user_identity(self):
        """Test SessionManager creates session with user identity."""
        manager = SessionManager()

        session_id = manager.create_session(
            data={"test": "data"},
            user_identity="user1@example.com",
            user_token="test-token",
            metadata={"source": "test"},
            token_claims={},
        )

        session = manager.get_session(session_id)
        assert session.user_identity == "user1@example.com"

    def test_multiple_sessions_different_users(self):
        """Test creating multiple sessions with different user identities."""
        manager = SessionManager()

        session_id_1 = manager.create_session(
            data={"user": 1}, user_identity="user1@example.com", user_token="test-token", token_claims={}
        )
        session_id_2 = manager.create_session(
            data={"user": 2}, user_identity="user2@example.com", user_token="test-token", token_claims={}
        )
        session_id_3 = manager.create_session(
            data={"user": 3}, user_identity="user1@example.com", user_token="test-token", token_claims={}
        )

        session_1 = manager.get_session(session_id_1)
        session_2 = manager.get_session(session_id_2)
        session_3 = manager.get_session(session_id_3)

        assert session_1.user_identity == "user1@example.com"
        assert session_2.user_identity == "user2@example.com"
        assert session_3.user_identity == "user1@example.com"

    def test_session_user_identity_persists(self):
        """Test that user identity persists across session retrieval."""
        manager = SessionManager()

        session_id = manager.create_session(
            data={"initial": "data"},
            user_identity="persistent@example.com",
            user_token="test-token",
            token_claims={},
        )

        # Retrieve session multiple times
        session_1 = manager.get_session(session_id)
        session_2 = manager.get_session(session_id)

        assert session_1.user_identity == "persistent@example.com"
        assert session_2.user_identity == "persistent@example.com"


class TestUserTokenKernelPropagation:
    """Test that user token and identity are propagated to the Jupyter kernel."""

    def _make_mock_kernel(self):
        """Create a mock kernel manager and client pair."""
        km = MagicMock()
        km.kernel_spec.argv = ["/usr/bin/python"]
        km.start_kernel = AsyncMock()
        km.interrupt_kernel = MagicMock()
        km.shutdown_kernel = AsyncMock()
        km.cleanup_resources = AsyncMock()

        kc = MagicMock()
        kc.start_channels = MagicMock()
        kc.wait_for_ready = AsyncMock()
        kc.stop_channels = MagicMock()

        km.client = MagicMock(return_value=kc)
        return km, kc

    def _make_mock_kernel_client_with_capture(self):
        """Create a mock kernel client that captures execute() calls and times out on iopub."""
        captured_code: list[str] = []

        async def fake_get_iopub_msg(timeout=1.0):
            raise asyncio.TimeoutError

        kc = MagicMock()
        kc.start_channels = MagicMock()
        kc.wait_for_ready = AsyncMock()
        kc.stop_channels = MagicMock()
        kc.get_iopub_msg = fake_get_iopub_msg
        kc.execute = MagicMock(side_effect=lambda code: captured_code.append(code) or "msg-1")

        return kc, captured_code

    @pytest.mark.asyncio
    async def test_kernel_started_with_user_assertion_token(self):
        """USER_ASSERTION_TOKEN env var is set when a new kernel is started."""
        manager = SessionManager()
        km, kc = self._make_mock_kernel()

        with patch(
            "code_execution.code_execution.sessions.manager.AsyncKernelManager",
            return_value=km,
        ):
            await manager._get_or_create_kernel(
                "sess-1",
                user_token="my-bearer-token",
                user_identity="user@example.com",
            )

        call_kwargs = km.start_kernel.call_args.kwargs
        env = call_kwargs.get("env", {})
        assert env.get("USER_ASSERTION_TOKEN") == "my-bearer-token"

    @pytest.mark.asyncio
    async def test_kernel_started_with_user_identity(self):
        """USER_IDENTITY env var is set when a new kernel is started."""
        manager = SessionManager()
        km, kc = self._make_mock_kernel()

        with patch(
            "code_execution.code_execution.sessions.manager.AsyncKernelManager",
            return_value=km,
        ):
            await manager._get_or_create_kernel(
                "sess-2",
                user_token="token",
                user_identity="user@example.com",
            )

        call_kwargs = km.start_kernel.call_args.kwargs
        env = call_kwargs.get("env", {})
        assert env.get("USER_IDENTITY") == "user@example.com"

    @pytest.mark.asyncio
    async def test_kernel_started_without_token_when_none(self):
        """USER_ASSERTION_TOKEN is NOT set when no token is provided."""
        manager = SessionManager()
        km, kc = self._make_mock_kernel()

        with patch(
            "code_execution.code_execution.sessions.manager.AsyncKernelManager",
            return_value=km,
        ):
            await manager._get_or_create_kernel("sess-3")

        call_kwargs = km.start_kernel.call_args.kwargs
        env = call_kwargs.get("env", {})
        assert "USER_ASSERTION_TOKEN" not in env

    @pytest.mark.asyncio
    async def test_kernel_token_tracked_after_start(self):
        """_kernel_tokens tracks the token used when the kernel was started."""
        manager = SessionManager()
        km, kc = self._make_mock_kernel()

        with patch(
            "code_execution.code_execution.sessions.manager.AsyncKernelManager",
            return_value=km,
        ):
            await manager._get_or_create_kernel("sess-4", user_token="initial-token")

        assert manager._kernel_tokens.get("sess-4") == "initial-token"

    @pytest.mark.asyncio
    async def test_execute_code_injects_updated_token(self):
        """If the session token changed, execute_code_for_session prepends an env update."""
        manager = SessionManager()

        # Create a real session with a token
        session_id = manager.create_session(
            data={},
            user_identity="user@example.com",
            user_token="new-token",
            token_claims={},
        )

        # Pre-populate the kernel registry with a mock and an outdated token
        km, _ = self._make_mock_kernel()
        kc, captured_code = self._make_mock_kernel_client_with_capture()

        manager._kernels[session_id] = (km, kc)
        manager._kernel_last_used[session_id] = 0.0
        manager._kernel_tokens[session_id] = "old-token"  # Simulate stale token

        try:
            await manager.execute_code_for_session(session_id, "print('hello')", timeout=1.0)
        except Exception:
            pass  # Timeout is expected since kc is mocked

        assert len(captured_code) == 1
        executed = captured_code[0]
        assert "USER_ASSERTION_TOKEN" in executed
        assert "new-token" in executed
        assert "print('hello')" in executed

    @pytest.mark.asyncio
    async def test_execute_code_no_preamble_when_token_unchanged(self):
        """No token preamble is prepended when the token hasn't changed."""
        manager = SessionManager()

        session_id = manager.create_session(
            data={},
            user_identity="user@example.com",
            user_token="same-token",
            token_claims={},
        )

        km, _ = self._make_mock_kernel()
        kc, captured_code = self._make_mock_kernel_client_with_capture()

        manager._kernels[session_id] = (km, kc)
        manager._kernel_last_used[session_id] = 0.0
        manager._kernel_tokens[session_id] = "same-token"  # Token matches session

        try:
            await manager.execute_code_for_session(session_id, "print('hello')", timeout=1.0)
        except Exception:
            pass  # Timeout is expected

        assert len(captured_code) == 1
        executed = captured_code[0]
        assert "USER_ASSERTION_TOKEN" not in executed
        assert executed == "print('hello')"

    @pytest.mark.asyncio
    async def test_execute_code_clears_token_when_removed(self):
        """If the session token is cleared, a preamble removes it from the kernel env."""
        manager = SessionManager()

        # Create session with an empty token (simulates token being cleared)
        session_id = manager.create_session(
            data={},
            user_identity="user@example.com",
            user_token="",
            token_claims={},
        )

        km, _ = self._make_mock_kernel()
        kc, captured_code = self._make_mock_kernel_client_with_capture()

        manager._kernels[session_id] = (km, kc)
        manager._kernel_last_used[session_id] = 0.0
        manager._kernel_tokens[session_id] = "previously-valid-token"

        try:
            await manager.execute_code_for_session(session_id, "print('hello')", timeout=1.0)
        except Exception:
            pass  # Timeout is expected since kc is mocked

        assert len(captured_code) == 1
        executed = captured_code[0]
        assert "del __agora_os__.environ['USER_ASSERTION_TOKEN']" in executed
        assert "print('hello')" in executed
        assert session_id not in manager._kernel_tokens

    @pytest.mark.asyncio
    async def test_execute_code_returns_notice_when_session_missing(self):
        """Missing/expired sessions should return a notice instead of running."""
        manager = SessionManager()
        session_id = manager.create_session(
            data={},
            user_identity="user@example.com",
            user_token="test-token",
            token_claims={},
        )
        manager.storage.delete(session_id)

        stdout, stderr, success, _displays = await manager.execute_code_for_session(session_id, "print('hello')", timeout=1.0)

        assert success is False
        assert stdout == ""
        assert "no longer available" in stderr

    @pytest.mark.asyncio
    async def test_execute_code_touches_session_during_long_running_execution(self):
        """Session keepalive should touch active sessions while code is still running."""
        manager = SessionManager(SessionConfig(timeout_minutes=0.2))  # type: ignore[arg-type]
        simulated_execution_delay_seconds = manager.execution_session_keepalive_seconds + 0.2
        session_id = manager.create_session(
            data={},
            user_identity="user@example.com",
            user_token="test-token",
            token_claims={},
        )

        km, _ = self._make_mock_kernel()
        kc = MagicMock()
        kc.start_channels = MagicMock()
        kc.wait_for_ready = AsyncMock()
        kc.stop_channels = MagicMock()
        kc.execute = MagicMock(return_value="msg-1")

        call_count = 0

        async def fake_get_iopub_msg(timeout=1.0):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                await asyncio.sleep(simulated_execution_delay_seconds)
                raise queue.Empty
            if call_count == 2:
                return {
                    "msg_type": "status",
                    "content": {"execution_state": "idle"},
                    "parent_header": {"msg_id": "msg-1"},
                }
            raise queue.Empty

        kc.get_iopub_msg = fake_get_iopub_msg

        manager._kernels[session_id] = (km, kc)
        manager._kernel_last_used[session_id] = 0.0
        manager._kernel_tokens[session_id] = "test-token"

        session = manager.storage.retrieve(session_id)
        assert session is not None
        with patch.object(session, "touch", wraps=session.touch) as touch_spy:
            stdout, stderr, success, _displays = await manager.execute_code_for_session(session_id, "print('hello')", timeout=5.0)

        assert success is True
        assert stdout == ""
        assert stderr == ""
        assert call_count >= 2
        assert touch_spy.call_count >= 2

    @pytest.mark.asyncio
    async def test_shutdown_kernel_clears_token_tracking(self):
        """_shutdown_kernel removes the entry from _kernel_tokens."""
        manager = SessionManager()

        km, kc = self._make_mock_kernel()
        manager._kernels["sess-x"] = (km, kc)
        manager._kernel_last_used["sess-x"] = 0.0
        manager._kernel_tokens["sess-x"] = "some-token"

        await manager._shutdown_kernel("sess-x")

        assert "sess-x" not in manager._kernel_tokens
        assert "sess-x" not in manager._kernels

    @pytest.mark.asyncio
    async def test_background_job_submission_and_completion(self):
        """Background execution returns a job id and transitions to completed status."""
        manager = SessionManager()
        session_id = manager.create_session(
            data={},
            user_identity="user@example.com",
            user_token="test-token",
            token_claims={},
        )

        km, _ = self._make_mock_kernel()
        kc = MagicMock()
        kc.execute = MagicMock(return_value="msg-1")
        kc.stop_channels = MagicMock()

        messages = [
            {
                "msg_type": "stream",
                "content": {"name": "stdout", "text": "step 1\n"},
                "parent_header": {"msg_id": "msg-1"},
            },
            {
                "msg_type": "status",
                "content": {"execution_state": "idle"},
                "parent_header": {"msg_id": "msg-1"},
            },
        ]

        async def fake_get_iopub_msg(timeout=1.0):
            if messages:
                return messages.pop(0)
            raise queue.Empty

        kc.get_iopub_msg = fake_get_iopub_msg

        manager._kernels[session_id] = (km, kc)
        manager._kernel_last_used[session_id] = 0.0
        manager._kernel_tokens[session_id] = "test-token"

        started = await manager.start_background_execution_for_session(session_id, "print('hello')", timeout=5.0)
        assert started["job_id"].startswith("j_")
        assert started["status"] == "running"

        status = None
        for _ in range(30):
            status = manager.check_background_job(started["job_id"])
            if status["status"] != "running":
                break
            await asyncio.sleep(0.01)

        assert status is not None
        assert status["status"] == "completed"
        assert status["success"] is True
        assert "step 1" in status["stdout"]

    @pytest.mark.asyncio
    async def test_execute_code_returns_busy_when_background_job_running(self):
        """Foreground execute is rejected while a background job is still running."""
        manager = SessionManager()
        session_id = manager.create_session(
            data={},
            user_identity="user@example.com",
            user_token="test-token",
            token_claims={},
        )

        km, _ = self._make_mock_kernel()
        kc = MagicMock()
        kc.execute = MagicMock(return_value="msg-1")
        kc.stop_channels = MagicMock()

        async def fake_get_iopub_msg(timeout=1.0):
            await asyncio.sleep(0.2)
            raise queue.Empty

        kc.get_iopub_msg = fake_get_iopub_msg

        manager._kernels[session_id] = (km, kc)
        manager._kernel_last_used[session_id] = 0.0
        manager._kernel_tokens[session_id] = "test-token"

        started = await manager.start_background_execution_for_session(session_id, "print('long run')", timeout=5.0)

        stdout, stderr, success, _displays = await manager.execute_code_for_session(session_id, "print('blocked')", timeout=1.0)
        assert success is False
        assert stdout == ""
        assert f"Session busy — job {started['job_id']} is still running" in stderr

        await manager._shutdown_kernel(session_id)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
