"""Unit and integration tests for session management infrastructure."""

import asyncio
import threading
import pytest
import time

from ...code_execution.sessions import (
    MaxSessionsReachedError,
    Session,
    SessionManager,
    SessionConfig,
    InMemoryStorage,
    get_current_session,
    set_current_session,
    SessionNotFound,
)


class TestSession:
    """Tests for Session class."""

    def test_session_creation(self):
        """Test basic session creation."""
        data = {"key": "value"}
        session = Session(
            session_id="test-123",
            data=data,
            session_type="test",
            user_identity="test_user",
            user_token="test-token",
            token_claims={},
        )

        assert session.session_id == "test-123"
        assert session.data == data
        assert session.session_type == "test"
        assert session.status == "created"

    def test_session_info(self):
        """Test session info generation."""
        session = Session(
            session_id="test-123",
            data={"test": "data"},
            session_type="test",
            user_identity="test_user",
            user_token="test-token",
            token_claims={},
            metadata={"user": "tester"},
        )

        info = session.get_info()
        assert info["session_id"] == "test-123"
        assert info["session_type"] == "test"
        assert info["status"] == "created"
        assert info["metadata"]["user"] == "tester"
        assert "age_seconds" in info
        assert "idle_seconds" in info

    def test_session_status_update(self):
        """Test status updates with history."""
        session = Session(
            session_id="test-123",
            data={},
            session_type="test",
            user_identity="test_user",
            user_token="test-token",
            token_claims={},
        )

        session.update_status("running")
        assert session.status == "running"

        session.update_status("completed")
        assert session.status == "completed"

        info = session.get_info()
        statuses = [h["status"] for h in info["status_history"]]
        assert statuses == ["created", "running", "completed"]

    def test_session_touch(self):
        """Test last accessed timestamp update."""
        session = Session(
            session_id="test-123",
            data={},
            session_type="test",
            user_identity="test_user",
            user_token="test-token",
            token_claims={},
        )

        initial_accessed = session.last_accessed
        time.sleep(0.01)
        session.touch()

        assert session.last_accessed > initial_accessed


class TestInMemoryStorage:
    """Tests for InMemoryStorage backend."""

    def test_store_and_retrieve(self):
        """Test storing and retrieving sessions."""
        storage = InMemoryStorage()
        session = Session("test-1", {"data": 1}, "test", "test_user", "test-token", {})

        storage.store("test-1", session)
        retrieved = storage.retrieve("test-1")

        assert retrieved is not None
        assert retrieved.session_id == "test-1"
        assert retrieved.data == {"data": 1}

    def test_retrieve_nonexistent(self):
        """Test retrieving non-existent session."""
        storage = InMemoryStorage()
        result = storage.retrieve("nonexistent")
        assert result is None

    def test_delete(self):
        """Test session deletion."""
        storage = InMemoryStorage()
        session = Session("test-1", {}, "test", "test_user", "test-token", {})

        storage.store("test-1", session)
        assert storage.count() == 1

        storage.delete("test-1")
        assert storage.count() == 0
        assert storage.retrieve("test-1") is None

    def test_list_all(self):
        """Test listing all sessions."""
        storage = InMemoryStorage()

        for i in range(3):
            session = Session(f"test-{i}", {"n": i}, "test", "test_user", "test-token", {})
            storage.store(f"test-{i}", session)

        all_sessions = storage.list_all()
        assert len(all_sessions) == 3
        assert "test-0" in all_sessions
        assert "test-1" in all_sessions
        assert "test-2" in all_sessions

    def test_count(self):
        """Test session counting."""
        storage = InMemoryStorage()
        assert storage.count() == 0

        storage.store("test-1", Session("test-1", {}, "test", "test_user", "test-token", {}))
        assert storage.count() == 1

        storage.store("test-2", Session("test-2", {}, "test", "test_user", "test-token", {}))
        assert storage.count() == 2


class TestSessionManager:
    """Tests for SessionManager."""

    def test_create_session(self):
        """Test session creation."""
        manager = SessionManager()
        data = {"test": "data"}

        session_id = manager.create_session(
            data=data,
            user_identity="tester",
            user_token="test-token",
            metadata={"user": "tester"},
            token_claims={},
        )

        assert session_id is not None
        session = manager.get_session(session_id)
        assert session.data == data
        assert session.metadata["user"] == "tester"

    def test_create_with_custom_id(self):
        """Test session creation with custom ID."""
        manager = SessionManager()

        session_id = manager.create_session(
            data={"test": "data"},
            user_identity="test_user",
            user_token="test-token",
            session_id="custom-id-123",
            token_claims={},
        )

        assert session_id == "custom-id-123"

    def test_get_nonexistent_session(self):
        """Test getting non-existent session raises error."""
        manager = SessionManager()

        with pytest.raises(ValueError, match="Session .* not found"):
            manager.get_session("nonexistent")

    def test_update_status(self):
        """Test status update through manager."""
        manager = SessionManager()
        session_id = manager.create_session({}, user_identity="test_user", user_token="test-token", token_claims={})

        manager.update_status(session_id, "running")
        session = manager.get_session(session_id)
        assert session.status == "running"

    def test_close_session(self):
        """Test explicit session closure."""
        manager = SessionManager()
        session_id = manager.create_session({}, user_identity="test_user", user_token="test-token", token_claims={})

        manager.close_session(session_id)

        with pytest.raises(ValueError):
            manager.get_session(session_id)

    def test_list_sessions(self):
        """Test listing sessions."""
        manager = SessionManager()

        manager.create_session({"a": 1}, user_identity="test_user", user_token="test-token", token_claims={})
        manager.create_session({"b": 2}, user_identity="test_user", user_token="test-token", token_claims={})
        manager.create_session({"a": 3}, user_identity="test_user", user_token="test-token", token_claims={})

        # List all
        all_sessions = manager.list_sessions()
        assert len(all_sessions) == 3

    def test_max_sessions_limit(self):
        """Test max sessions enforcement rejects new sessions."""
        config = SessionConfig(max_sessions=3)
        manager = SessionManager(config)

        # Create 3 sessions (at limit)
        id1 = manager.create_session({"n": 1}, user_identity="test_user", user_token="test-token", token_claims={})
        id2 = manager.create_session({"n": 2}, user_identity="test_user", user_token="test-token", token_claims={})
        id3 = manager.create_session({"n": 3}, user_identity="test_user", user_token="test-token", token_claims={})

        assert manager.storage.count() == 3

        # 4th session should be rejected
        with pytest.raises(MaxSessionsReachedError, match="Maximum number of sessions"):
            manager.create_session({"n": 4}, user_identity="test_user", user_token="test-token", token_claims={})

        # All original sessions should still exist
        assert manager.storage.count() == 3
        assert manager.get_session(id1) is not None
        assert manager.get_session(id2) is not None
        assert manager.get_session(id3) is not None

    def test_create_session_enforces_limit_atomically_under_concurrency(self, monkeypatch):
        """Concurrent create_session calls should not exceed max_sessions."""
        manager = SessionManager(SessionConfig(max_sessions=1))
        original_store = manager.storage.store
        first_store_entered = threading.Event()
        release_first_store = threading.Event()
        store_call_count = 0
        store_counter_lock = threading.Lock()

        def _controlled_store(session_id, session):
            nonlocal store_call_count
            with store_counter_lock:
                store_call_count += 1
                call_number = store_call_count
            if call_number == 1:
                first_store_entered.set()
                release_first_store.wait(timeout=2)
            original_store(session_id, session)

        monkeypatch.setattr(manager.storage, "store", _controlled_store)

        results: list[str] = []
        errors: list[Exception] = []

        def _create():
            try:
                session_id = manager.create_session({}, user_identity="test_user", user_token="test-token", token_claims={})
                results.append(session_id)
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=_create)
        t2 = threading.Thread(target=_create)
        t1.start()
        assert first_store_entered.wait(timeout=2)
        t2.start()
        release_first_store.set()
        t1.join(timeout=2)
        t2.join(timeout=2)

        assert len(results) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], MaxSessionsReachedError)
        assert manager.storage.count() == 1

    def test_session_timeout(self):
        """Test session timeout cleanup."""
        config = SessionConfig(
            timeout_minutes=0.001,  # ~0.06 seconds  # type: ignore[arg-type]
            cleanup_interval_seconds=0.001,  # type: ignore[arg-type]
        )
        manager = SessionManager(config)

        # Create session
        session_id = manager.create_session(
            {"test": "data"}, user_identity="test_user", user_token="test-token", token_claims={}
        )

        # Wait for timeout
        time.sleep(0.1)

        # Trigger cleanup by creating another session
        manager.create_session({"new": "data"}, user_identity="test_user", user_token="test-token", token_claims={})

        # Original session should be gone
        with pytest.raises(ValueError):
            manager.get_session(session_id)


class TestContext:
    """Tests for context-based session injection."""

    def test_get_without_set_raises_error(self):
        """Test getting session without setting raises error."""
        # Clear any existing context
        set_current_session(None)

        with pytest.raises(SessionNotFound, match="No active session"):
            get_current_session()

    def test_set_and_get_session(self):
        """Test setting and getting session."""
        session = Session("test-123", {"data": "value"}, "test", "test_user", "test-token", {})

        set_current_session(session)
        retrieved = get_current_session()

        assert retrieved.session_id == "test-123"
        assert retrieved.data == {"data": "value"}

        # Cleanup
        set_current_session(None)

    def test_clear_session(self):
        """Test clearing session context."""
        session = Session("test-123", {}, "test", "test_user", "test-token", {})
        set_current_session(session)

        set_current_session(None)

        with pytest.raises(SessionNotFound):
            get_current_session()


@pytest.mark.asyncio
class TestSessionIntegration:
    """Integration tests for session functionality with code execution."""

    async def _execute_in_session(self, test_server, session_id: str, code: str, timeout: int = 10):
        """
        Helper to execute code within a session context.

        This simulates what the MCP tool layer does: retrieves the session,
        sets context, executes code, and saves the session.
        """
        return await test_server.execute_code_with_session(code=code, timeout=timeout, session_id=session_id)

    async def test_manual_session_creation_and_execution(self, test_server):
        """Test that sessions can be manually created and used for code execution."""
        # Clear any existing sessions
        for session_info in test_server.session_manager.list_sessions():
            test_server.session_manager.close_session(session_info["session_id"])

        # Manually create a session
        session_id = test_server.session_manager.create_session(
            data={}, user_identity="test_user", user_token="test-token", token_claims={}
        )
        session = test_server.session_manager.get_session(session_id)
        set_current_session(session)

        # Execute code (with session context)
        result = await test_server.execute_code_with_session(code="x = 42", timeout=10, session_id=session_id)

        assert result.success, f"Execution failed: {result.error}\nStderr: {result.stderr}"

        # Verify session exists and is tracked
        sessions = test_server.session_manager.list_sessions()
        assert len(sessions) == 1, f"Expected 1 session, found {len(sessions)}"
        assert sessions[0]["session_id"] == session_id
        assert sessions[0]["status"] == "created"

    async def test_state_persistence_across_calls(self, test_server):
        """Test that variables persist across multiple executions."""
        # Clear any existing sessions
        for session_info in test_server.session_manager.list_sessions():
            test_server.session_manager.close_session(session_info["session_id"])

        # Create session
        session_id = test_server.session_manager.create_session(
            data={}, user_identity="test_user", user_token="test-token", token_claims={}
        )

        # First execution: set variable
        result1 = await self._execute_in_session(test_server, session_id, "test_var = 'hello'")
        assert result1.success, f"Execution failed: {result1.error}\nStderr: {result1.stderr}"

        # Second execution: use the variable
        result2 = await self._execute_in_session(test_server, session_id, "print(test_var)")
        assert result2.success
        assert "hello" in result2.stdout

        # Third execution: modify and use variable
        result3 = await self._execute_in_session(
            test_server, session_id, "test_var = test_var + ' world'; print(test_var)"
        )
        assert result3.success
        assert "hello world" in result3.stdout

    async def test_numeric_state_persistence(self, test_server):
        """Test that numeric computations persist across calls."""
        # Clear sessions
        for session_info in test_server.session_manager.list_sessions():
            test_server.session_manager.close_session(session_info["session_id"])

        # Create session
        session_id = test_server.session_manager.create_session(
            data={}, user_identity="test_user", user_token="test-token", token_claims={}
        )

        # First: Define variables
        result1 = await self._execute_in_session(test_server, session_id, "a = 10; b = 20")
        assert result1.success, f"Execution failed: {result1.error}\nStderr: {result1.stderr}"

        # Second: Compute with persisted variables
        result2 = await self._execute_in_session(test_server, session_id, "c = a + b; print(c)")
        assert result2.success
        assert "30" in result2.stdout

        # Third: Verify all variables still exist
        result3 = await self._execute_in_session(test_server, session_id, "print(f'{a}, {b}, {c}')")
        assert result3.success
        assert "10, 20, 30" in result3.stdout

    async def test_function_persistence(self, test_server):
        """Test that function definitions persist across calls."""
        # Clear sessions
        for session_info in test_server.session_manager.list_sessions():
            test_server.session_manager.close_session(session_info["session_id"])

        # Create session
        session_id = test_server.session_manager.create_session(
            data={}, user_identity="test_user", user_token="test-token", token_claims={}
        )

        # First: Define a function
        result1 = await self._execute_in_session(test_server, session_id, "def greet(name): return f'Hello, {name}!'")
        assert result1.success

        # Second: Call the function (should persist)
        result2 = await self._execute_in_session(test_server, session_id, "print(greet('World'))")
        assert result2.success
        assert "Hello, World!" in result2.stdout

    async def test_import_persistence(self, test_server):
        """Test import behavior with namespace persistence."""
        # Clear sessions
        for session_info in test_server.session_manager.list_sessions():
            test_server.session_manager.close_session(session_info["session_id"])

        # Create session
        session_id = test_server.session_manager.create_session(
            data={}, user_identity="test_user", user_token="test-token", token_claims={}
        )

        # First: Import module
        result1 = await self._execute_in_session(test_server, session_id, "import math")
        assert result1.success

        # Second: Use the imported module without re-importing (should persist)
        result2 = await self._execute_in_session(test_server, session_id, "print(math.pi)")
        assert result2.success
        assert "3.14" in result2.stdout

    async def test_class_instance_persistence(self, test_server):
        """Test that class instances persist across calls."""
        # Clear sessions
        for session_info in test_server.session_manager.list_sessions():
            test_server.session_manager.close_session(session_info["session_id"])

        # Create session
        session_id = test_server.session_manager.create_session(
            data={}, user_identity="test_user", user_token="test-token", token_claims={}
        )

        # First: Define class and create instance
        result1 = await self._execute_in_session(
            test_server,
            session_id,
            """
class Counter:
    def __init__(self):
        self.count = 0
    def increment(self):
        self.count += 1
        return self.count

counter = Counter()
print(counter.increment())
                """,
        )
        assert result1.success, f"Failed: {result1.stderr}"
        assert "1" in result1.stdout

        # Second: Use the persisted instance
        result2 = await self._execute_in_session(test_server, session_id, "print(counter.increment())")
        assert result2.success
        assert "2" in result2.stdout

        # Third: Increment again
        result3 = await self._execute_in_session(test_server, session_id, "print(counter.increment())")
        assert result3.success
        assert "3" in result3.stdout

    async def test_session_isolation(self, test_server):
        """Test that different sessions are isolated from each other."""
        # Clear sessions
        for session_info in test_server.session_manager.list_sessions():
            test_server.session_manager.close_session(session_info["session_id"])

        # Create first session
        session1_id = test_server.session_manager.create_session(
            data={}, user_identity="test_user", user_token="test-token", token_claims={}
        )

        result1 = await self._execute_in_session(test_server, session1_id, "session1_var = 'first'")
        assert result1.success

        # Create second session
        session2_id = test_server.session_manager.create_session(
            data={}, user_identity="test_user", user_token="test-token", token_claims={}
        )

        result2 = await self._execute_in_session(test_server, session2_id, "session2_var = 'second'")
        assert result2.success

        # Verify session 1 doesn't have session2_var
        result3 = await self._execute_in_session(test_server, session1_id, "print('session1_var' in dir())")
        assert "True" in result3.stdout

        result4 = await self._execute_in_session(test_server, session1_id, "print('session2_var' in dir())")
        assert "False" in result4.stdout

        # Verify session 2 doesn't have session1_var
        result5 = await self._execute_in_session(test_server, session2_id, "print('session2_var' in dir())")
        assert "True" in result5.stdout

        result6 = await self._execute_in_session(test_server, session2_id, "print('session1_var' in dir())")
        assert "False" in result6.stdout

    async def test_session_timeout(self):
        """Test that sessions timeout after inactivity."""
        # Create a separate session manager with fast timeout for this test
        fast_manager = SessionManager(
            SessionConfig(
                max_sessions=10,
                timeout_minutes=0.01,  # type: ignore[arg-type]
                cleanup_interval_seconds=1,  # Check every second
            )
        )

        # Create session
        fast_manager.create_session(data={}, user_identity="test_user", user_token="test-token", token_claims={})
        sessions = fast_manager.list_sessions()
        assert len(sessions) == 1

        # Wait for timeout
        await asyncio.sleep(2)

        # Session should be cleaned up
        sessions_after = fast_manager.list_sessions()
        assert len(sessions_after) == 0, f"Expected session to be cleaned up, but {len(sessions_after)} sessions remain"

    async def test_namespace_serialization_with_complex_types(self, test_server):
        """Test that various Python types serialize/deserialize correctly."""
        # Clear sessions
        for session_info in test_server.session_manager.list_sessions():
            test_server.session_manager.close_session(session_info["session_id"])

        # Create session
        session_id = test_server.session_manager.create_session(
            data={}, user_identity="test_user", user_token="test-token", token_claims={}
        )

        # First: Create various types
        result1 = await self._execute_in_session(
            test_server,
            session_id,
            "my_list = [1, 2, 3]; my_dict = {'key': 'value', 'nested': {'a': 1}}; my_tuple = (4, 5, 6); my_set = {7, 8, 9}",
        )
        assert result1.success, f"Failed: {result1.stderr}"

        # Second: Verify all types persisted
        result2 = await self._execute_in_session(
            test_server,
            session_id,
            "print(my_list); print(my_dict); print(my_tuple); print(my_set)",
        )
        assert result2.success
        assert "[1, 2, 3]" in result2.stdout
        assert "'key': 'value'" in result2.stdout
        assert "(4, 5, 6)" in result2.stdout
        # Sets have unpredictable order in output
        assert "7" in result2.stdout and "8" in result2.stdout and "9" in result2.stdout


class TestDisplayDataCapture:
    """The kernel polling loop must capture Jupyter display_data / execute_result
    rich outputs (matplotlib figures, images, SVGs) so the activity UI can
    render them.  Text-only payloads must NOT be returned as displays — they
    are already in stdout.
    """

    @pytest.mark.unit
    def test_extract_display_prefers_png(self):
        from ...code_execution.sessions.manager import _extract_display

        out = _extract_display(
            {"text/plain": "<Figure>", "image/png": "BASE64HERE"},
            {"width": 800},
        )
        assert out == {"mime_type": "image/png", "data": "BASE64HERE", "metadata": {"width": 800}}

    @pytest.mark.unit
    def test_extract_display_falls_back_to_svg(self):
        from ...code_execution.sessions.manager import _extract_display

        out = _extract_display(
            {"text/plain": "<Figure>", "image/svg+xml": "<svg/>"},
            {},
        )
        assert out is not None
        assert out["mime_type"] == "image/svg+xml"
        assert out["data"] == "<svg/>"

    @pytest.mark.unit
    def test_extract_display_text_only_returns_none(self):
        """Pure text/plain payloads belong in stdout, not displays."""
        from ...code_execution.sessions.manager import _extract_display

        assert _extract_display({"text/plain": "just text"}, {}) is None
        assert _extract_display({}, {}) is None
        assert _extract_display(None, None) is None  # type: ignore[arg-type]

    @pytest.mark.unit
    def test_extract_display_html_third_priority(self):
        """text/html ranks below image formats but above plain text."""
        from ...code_execution.sessions.manager import _extract_display

        out = _extract_display(
            {"text/plain": "<DataFrame>", "text/html": "<table></table>"},
            {},
        )
        assert out is not None
        assert out["mime_type"] == "text/html"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
