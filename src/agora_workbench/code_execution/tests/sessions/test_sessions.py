"""Unit and integration tests for session management infrastructure."""

import asyncio
import threading
import pytest
import time

from ...data_access.manager import DataLakeDataManager
from ...sessions import (
    MaxSessionsReachedError,
    Session,
    SessionManager,
    SessionConfig,
    SessionContext,
    InMemoryStorage,
    get_current_session,
    set_current_session,
    SessionNotFound,
)


class _FakeDataManager(DataLakeDataManager):
    """Stand-in for DataLakeDataManager that records cleanup.

    Deliberately does not call ``super().__init__()``, so no temp cache
    directory is allocated — the tests below assert on temp-dir allocation.
    """

    def __init__(self):  # noqa: D107 - intentionally skips the base initializer
        self.cleanup_calls = 0

    def cleanup(self) -> None:
        self.cleanup_calls += 1


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
                session_id = manager.create_session(
                    {}, user_identity="test_user", user_token="test-token", token_claims={}
                )
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
        from ...sessions.manager import _extract_display

        out = _extract_display(
            {"text/plain": "<Figure>", "image/png": "BASE64HERE"},
            {"width": 800},
        )
        assert out == {"mime_type": "image/png", "data": "BASE64HERE", "metadata": {"width": 800}}

    @pytest.mark.unit
    def test_extract_display_falls_back_to_svg(self):
        from ...sessions.manager import _extract_display

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
        from ...sessions.manager import _extract_display

        assert _extract_display({"text/plain": "just text"}, {}) is None
        assert _extract_display({}, {}) is None
        assert _extract_display(None, None) is None  # type: ignore[arg-type]

    @pytest.mark.unit
    def test_extract_display_drops_html(self):
        """text/html is intentionally NOT a capturable display type.

        Rendering raw HTML in the activity UI is an XSS vector because
        kernel code is LLM-generated and prompt-injectable.  HTML-only
        payloads (e.g. a DataFrame ``_repr_html_``) fall through to None
        so the UI shows nothing for them.
        """
        from ...sessions.manager import _extract_display

        assert (
            _extract_display(
                {"text/plain": "<DataFrame>", "text/html": "<table></table>"},
                {},
            )
            is None
        )
        assert _extract_display({"text/html": "<script>alert(1)</script>"}, {}) is None


class TestKernelExecuteLock:
    """Per-session asyncio.Lock that serializes execute_code_for_session calls.

    Background: the Jupyter ``KernelClient`` exposes a single iopub queue.
    Two coroutines polling that queue concurrently can each consume the
    other's reply messages — the loser then spins until its timeout fires.
    The lock prevents two ``execute_code_for_session`` bodies from running
    against the same kernel client at the same time.
    """

    @pytest.mark.unit
    def test_lock_is_per_session_and_stable(self):
        """The same session_id returns the same lock; different ids do not."""
        manager = SessionManager()
        lock_a1 = manager._get_kernel_execute_lock("session-a")
        lock_a2 = manager._get_kernel_execute_lock("session-a")
        lock_b = manager._get_kernel_execute_lock("session-b")
        assert lock_a1 is lock_a2
        assert lock_a1 is not lock_b

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_concurrent_calls_serialize_on_same_session(self, monkeypatch):
        """Two concurrent execute_code_for_session calls for the same session
        must not run their inner kernel work in parallel."""
        manager = SessionManager()
        # Inject a fake session so the lookup at the top of
        # execute_code_for_session succeeds.
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})

        in_flight = 0
        max_in_flight = 0
        order: list[str] = []

        async def fake_locked(*, code, **_kwargs):
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            order.append(f"start:{code}")
            # Yield to the event loop so the other coroutine has a chance to
            # try and (incorrectly) enter the critical section.
            await asyncio.sleep(0.02)
            order.append(f"end:{code}")
            in_flight -= 1
            return ("", "", True, [])

        monkeypatch.setattr(manager, "_execute_code_locked", fake_locked)

        await asyncio.gather(
            manager.execute_code_for_session(session_id, "A", timeout=5.0),
            manager.execute_code_for_session(session_id, "B", timeout=5.0),
        )

        # If serialization works, max_in_flight never exceeds 1 and each call
        # completes before the next one starts.
        assert max_in_flight == 1, f"calls overlapped (max_in_flight={max_in_flight})"
        assert order == ["start:A", "end:A", "start:B", "end:B"] or order == [
            "start:B",
            "end:B",
            "start:A",
            "end:A",
        ], f"unexpected interleave: {order}"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_concurrent_calls_overlap_on_different_sessions(self, monkeypatch):
        """Different sessions get separate locks; calls must overlap freely."""
        manager = SessionManager()
        sid_a = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        sid_b = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})

        in_flight = 0
        max_in_flight = 0

        async def fake_locked(**_kwargs):
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            await asyncio.sleep(0.05)
            in_flight -= 1
            return ("", "", True, [])

        monkeypatch.setattr(manager, "_execute_code_locked", fake_locked)

        await asyncio.gather(
            manager.execute_code_for_session(sid_a, "A", timeout=5.0),
            manager.execute_code_for_session(sid_b, "B", timeout=5.0),
        )

        # Different sessions => different locks => calls overlap.
        assert max_in_flight == 2, f"calls did not overlap (max_in_flight={max_in_flight})"


class TestArtifactPipeline:
    """Per-session artifact discovery and download-token lifecycle.

    Each execute snapshots the session's outputs dir, runs the code, then
    diffs the dir to surface new/modified files as downloadable artifacts.
    These tests cover the diff/registration plumbing — they don't spin up a
    real Jupyter kernel.
    """

    def _make_manager(self, tmp_path, monkeypatch):
        """Build a SessionManager whose outputs dir is a temp path.

        Re-pointing the module-level constant via monkeypatch ensures the
        test never writes to ``~/agora-outputs``.
        """
        from ... import sessions as sessions_pkg

        monkeypatch.setattr(sessions_pkg.manager, "_OUTPUTS_BASE_DIR", tmp_path)
        return sessions_pkg.SessionManager()

    @pytest.mark.unit
    def test_create_session_makes_outputs_dir(self, tmp_path, monkeypatch):
        manager = self._make_manager(tmp_path, monkeypatch)
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        assert (tmp_path / session_id).is_dir()

    @pytest.mark.unit
    def test_snapshot_returns_empty_when_dir_missing(self, tmp_path, monkeypatch):
        manager = self._make_manager(tmp_path, monkeypatch)
        assert manager._snapshot_outputs_dir("nonexistent-session") == {}

    @pytest.mark.unit
    def test_diff_registers_new_file_with_token(self, tmp_path, monkeypatch):
        manager = self._make_manager(tmp_path, monkeypatch)
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        outputs = manager._get_outputs_dir(session_id)

        before = manager._snapshot_outputs_dir(session_id)
        (outputs / "results.csv").write_text("x,y\n1,2\n")
        after = manager._snapshot_outputs_dir(session_id)

        records = manager._register_artifacts_from_diff(session_id, before, after)
        assert len(records) == 1
        record = records[0]
        assert record["name"] == "results.csv"
        assert record["size_bytes"] == len("x,y\n1,2\n")
        assert record["mime_type"] == "text/csv"
        assert "download_token" in record

        # Token resolves to a usable record (handles the download endpoint).
        resolved = manager.get_artifact_record(session_id, record["download_token"])
        assert resolved is not None
        assert resolved.path == outputs / "results.csv"

    @pytest.mark.unit
    def test_diff_ignores_unchanged_files(self, tmp_path, monkeypatch):
        manager = self._make_manager(tmp_path, monkeypatch)
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        outputs = manager._get_outputs_dir(session_id)
        (outputs / "stable.txt").write_text("nope")

        # Same snapshot both times -> no diff -> no records.
        snapshot = manager._snapshot_outputs_dir(session_id)
        assert manager._register_artifacts_from_diff(session_id, snapshot, snapshot) == []

    @pytest.mark.unit
    def test_diff_picks_up_modifications(self, tmp_path, monkeypatch):
        manager = self._make_manager(tmp_path, monkeypatch)
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        outputs = manager._get_outputs_dir(session_id)
        (outputs / "changing.txt").write_text("v1")

        before = manager._snapshot_outputs_dir(session_id)
        # Make the file bigger and bump mtime to guarantee diff detection.
        (outputs / "changing.txt").write_text("v2-grew")
        import os as _os

        _os.utime(outputs / "changing.txt", (time.time() + 5, time.time() + 5))
        after = manager._snapshot_outputs_dir(session_id)

        records = manager._register_artifacts_from_diff(session_id, before, after)
        assert [r["name"] for r in records] == ["changing.txt"]

    @pytest.mark.unit
    def test_denylist_skips_pycache_and_hidden(self, tmp_path, monkeypatch):
        manager = self._make_manager(tmp_path, monkeypatch)
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        outputs = manager._get_outputs_dir(session_id)
        (outputs / "__pycache__").mkdir()
        (outputs / "__pycache__" / "junk.pyc").write_bytes(b"\x00")
        (outputs / "module.pyc").write_bytes(b"\x00")
        (outputs / ".hidden").write_text("nope")
        (outputs / "real.csv").write_text("yes")

        before = {}
        after = manager._snapshot_outputs_dir(session_id)
        records = manager._register_artifacts_from_diff(session_id, before, after)
        assert [r["name"] for r in records] == ["real.csv"]

    @pytest.mark.unit
    def test_get_artifact_record_returns_none_for_unknown_token(self, tmp_path, monkeypatch):
        manager = self._make_manager(tmp_path, monkeypatch)
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        assert manager.get_artifact_record(session_id, "nope") is None
        assert manager.get_artifact_record("missing-session", "any") is None

    @pytest.mark.unit
    def test_get_artifact_record_returns_none_when_file_deleted(self, tmp_path, monkeypatch):
        manager = self._make_manager(tmp_path, monkeypatch)
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        outputs = manager._get_outputs_dir(session_id)
        (outputs / "ephemeral.txt").write_text("bye")
        records = manager._register_artifacts_from_diff(session_id, {}, manager._snapshot_outputs_dir(session_id))
        token = records[0]["download_token"]
        (outputs / "ephemeral.txt").unlink()
        assert manager.get_artifact_record(session_id, token) is None

    @pytest.mark.unit
    def test_outputs_preamble_runs_once_per_session(self, tmp_path, monkeypatch):
        manager = self._make_manager(tmp_path, monkeypatch)
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        first = manager._prepare_outputs_preamble(session_id)
        second = manager._prepare_outputs_preamble(session_id)
        assert "AGORA_OUTPUT_DIR" in first
        assert second == ""

    @pytest.mark.unit
    def test_cleanup_drops_state_and_removes_dir(self, tmp_path, monkeypatch):
        manager = self._make_manager(tmp_path, monkeypatch)
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        outputs = manager._get_outputs_dir(session_id)
        (outputs / "foo.txt").write_text("data")
        manager._register_artifacts_from_diff(session_id, {}, manager._snapshot_outputs_dir(session_id))
        assert session_id in manager._session_artifacts

        manager._cleanup_session_artifacts(session_id)
        assert session_id not in manager._session_artifacts
        assert not outputs.exists()


class TestBackgroundArtifactPipeline:
    """Background-execute path surfaces files under AGORA_OUTPUT_DIR too.

    Mirrors :class:`TestArtifactPipeline` for the foreground path; together
    they guard the regression where the background path silently skipped
    the preamble + snapshot/diff plumbing, leaving the kernel with no
    ``AGORA_OUTPUT_DIR`` and the activity UI with no download links.
    """

    def _make_manager(self, tmp_path, monkeypatch):
        from ... import sessions as sessions_pkg

        monkeypatch.setattr(sessions_pkg.manager, "_OUTPUTS_BASE_DIR", tmp_path)
        return sessions_pkg.SessionManager()

    @pytest.mark.unit
    def test_background_job_carries_outputs_before_and_artifacts(self):
        from ...sessions.manager import _BackgroundJob

        job = _BackgroundJob(
            job_id="j",
            session_id="s",
            msg_id="m",
            timeout=1.0,
            start_time=0.0,
        )
        assert job.outputs_before == {}
        assert job.artifacts == []

    @pytest.mark.unit
    def test_finalize_registers_artifacts(self, tmp_path, monkeypatch):
        from ...sessions.manager import _BackgroundJob

        manager = self._make_manager(tmp_path, monkeypatch)
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        outputs = manager._get_outputs_dir(session_id)

        before = manager._snapshot_outputs_dir(session_id)
        (outputs / "out.csv").write_text("a,b\n1,2\n")

        job = _BackgroundJob(
            job_id="j",
            session_id=session_id,
            msg_id="m",
            timeout=1.0,
            start_time=0.0,
            outputs_before=before,
        )
        manager._finalize_background_artifacts(job)

        assert [a["name"] for a in job.artifacts] == ["out.csv"]
        assert "download_token" in job.artifacts[0]

    @pytest.mark.unit
    def test_finalize_no_changes_yields_empty(self, tmp_path, monkeypatch):
        from ...sessions.manager import _BackgroundJob

        manager = self._make_manager(tmp_path, monkeypatch)
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        before = manager._snapshot_outputs_dir(session_id)

        job = _BackgroundJob(
            job_id="j",
            session_id=session_id,
            msg_id="m",
            timeout=1.0,
            start_time=0.0,
            outputs_before=before,
        )
        manager._finalize_background_artifacts(job)
        assert job.artifacts == []

    @pytest.mark.unit
    def test_finalize_is_best_effort_on_snapshot_failure(self, tmp_path, monkeypatch):
        from ...sessions.manager import _BackgroundJob

        manager = self._make_manager(tmp_path, monkeypatch)
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})

        def boom(*_a, **_k):
            raise RuntimeError("simulated snapshot failure")

        monkeypatch.setattr(manager, "_snapshot_outputs_dir", boom)

        job = _BackgroundJob(
            job_id="j",
            session_id=session_id,
            msg_id="m",
            timeout=1.0,
            start_time=0.0,
        )
        manager._finalize_background_artifacts(job)
        assert job.artifacts == []

    @pytest.mark.unit
    def test_check_job_terminal_includes_artifacts_without_urls(self, tmp_path, monkeypatch):
        """Terminal-state result carries artifacts; URL composition is the
        server layer's job, exactly like the foreground execute contract."""
        from ...sessions.manager import _BackgroundJob

        manager = self._make_manager(tmp_path, monkeypatch)
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        job = _BackgroundJob(
            job_id="j_xyz",
            session_id=session_id,
            msg_id="m",
            timeout=1.0,
            start_time=0.0,
            status="completed",
            completed_at=0.5,
            artifacts=[
                {
                    "name": "x.csv",
                    "size_bytes": 4,
                    "mime_type": "text/csv",
                    "modified_at": "2026-01-01T00:00:00+00:00",
                    "download_token": "abc",
                }
            ],
        )
        manager._background_jobs["j_xyz"] = job

        result = manager.check_background_job("j_xyz")
        assert result["status"] == "completed"
        assert [a["name"] for a in result["artifacts"]] == ["x.csv"]
        assert "download_url" not in result["artifacts"][0]

    @pytest.mark.unit
    def test_check_job_running_omits_artifacts(self, tmp_path, monkeypatch):
        from ...sessions.manager import _BackgroundJob

        manager = self._make_manager(tmp_path, monkeypatch)
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        job = _BackgroundJob(
            job_id="j_run",
            session_id=session_id,
            msg_id="m",
            timeout=1.0,
            start_time=0.0,
            status="running",
        )
        manager._background_jobs["j_run"] = job

        result = manager.check_background_job("j_run")
        assert result["status"] == "running"
        assert "artifacts" not in result

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_start_background_seeds_outputs_preamble_and_snapshot(self, tmp_path, monkeypatch):
        """start_background_execution_for_session prepends the outputs preamble
        (so kernels whose FIRST execute is background still get
        AGORA_OUTPUT_DIR) and records the pre-execute snapshot on the job."""
        manager = self._make_manager(tmp_path, monkeypatch)
        session_id = manager.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        outputs = manager._get_outputs_dir(session_id)
        (outputs / "preexisting.txt").write_text("v1")

        captured = {}

        class _StubKC:
            def execute(self, code):
                captured["code"] = code
                return "msg-id-stub"

        async def _stub_get_or_create_kernel(*_a, **_k):
            return (object(), _StubKC())

        async def _stub_collect(*_a, **_k):
            return None

        monkeypatch.setattr(manager, "_get_or_create_kernel", _stub_get_or_create_kernel)
        monkeypatch.setattr(manager, "_collect_background_job", _stub_collect)

        result = await manager.start_background_execution_for_session(
            session_id, "print(AGORA_OUTPUT_DIR)", timeout=30.0
        )
        job_id = result["job_id"]
        job = manager._background_jobs[job_id]

        # Outputs preamble must precede the user code.
        assert "AGORA_OUTPUT_DIR" in captured["code"]
        assert captured["code"].index("AGORA_OUTPUT_DIR") < captured["code"].index("print(AGORA_OUTPUT_DIR)")
        # Pre-existing file is in the before-snapshot stored on the job.
        assert "preexisting.txt" in job.outputs_before


class TestDataManagerInjection:
    """Tests for injecting a custom DataLakeDataManager into sessions."""

    @staticmethod
    def _make_session(data_manager=None):
        return Session(
            session_id="sess-1",
            data={},
            session_type="test",
            user_identity="test_user",
            user_token="test-token",
            token_claims={},
            data_manager=data_manager,
        )

    def test_session_builds_default_manager_when_none_injected(self):
        """Default behavior is unchanged: the session constructs its own manager."""
        session = self._make_session()
        try:
            assert isinstance(session.data_manager, DataLakeDataManager)
            assert not isinstance(session.data_manager, _FakeDataManager)
        finally:
            session.cleanup()

    def test_session_uses_injected_manager(self, monkeypatch):
        """An injected manager is used verbatim, and no default is constructed."""
        from ...data_access import manager as manager_module

        constructed = []
        monkeypatch.setattr(
            manager_module,
            "DataLakeDataManager",
            lambda *a, **k: constructed.append(1),
        )

        injected = _FakeDataManager()
        session = self._make_session(data_manager=injected)

        assert session.data_manager is injected
        assert constructed == [], "default DataLakeDataManager must not be constructed"

    def test_session_cleanup_cleans_up_injected_manager(self):
        """The session owns its manager, injected or not."""
        injected = _FakeDataManager()
        session = self._make_session(data_manager=injected)

        session.cleanup()

        assert injected.cleanup_calls == 1

    def test_factory_receives_session_context(self):
        """The factory sees the identifying fields of the session being created."""
        seen: list[SessionContext] = []

        def factory(context):
            seen.append(context)
            return _FakeDataManager()

        session_manager = SessionManager(SessionConfig(data_manager_factory=factory))
        session_id = session_manager.create_session(
            {"payload": 1},
            user_identity="alice@tenant",
            user_token="alice-token",
            token_claims={"oid": "alice"},
            metadata={"tier": "premium"},
        )

        assert len(seen) == 1
        context = seen[0]
        assert context.session_id == session_id
        assert context.user_identity == "alice@tenant"
        assert context.user_token == "alice-token"
        assert context.token_claims == {"oid": "alice"}
        assert context.metadata == {"tier": "premium"}
        assert context.session_type == "default"

    def test_factory_result_is_attached_to_session(self):
        """The manager returned by the factory is the one the session uses."""
        built = _FakeDataManager()
        session_manager = SessionManager(SessionConfig(data_manager_factory=lambda _ctx: built))

        session_id = session_manager.create_session(
            {}, user_identity="test_user", user_token="test-token", token_claims={}
        )

        assert session_manager.get_session(session_id).data_manager is built

    def test_factory_avoids_constructing_default_manager(self, monkeypatch):
        """Regression: no throwaway default manager (and no leaked temp dir).

        ``DataLakeDataManager.__init__`` allocates a temp cache dir eagerly, so
        building one only to replace it leaks a directory per session. The
        factory must be consulted *before* the Session is constructed.
        """
        from ...data_access import manager as manager_module

        constructed = []
        monkeypatch.setattr(
            manager_module,
            "DataLakeDataManager",
            lambda *a, **k: constructed.append(1),
        )

        session_manager = SessionManager(SessionConfig(data_manager_factory=lambda _ctx: _FakeDataManager()))
        session_manager.create_session({}, user_identity="test_user", user_token="test-token", token_claims={})

        assert constructed == []

    def test_factory_invoked_once_per_session(self):
        """Each session gets its own manager instance from a fresh factory call."""
        session_manager = SessionManager(SessionConfig(data_manager_factory=lambda _ctx: _FakeDataManager()))

        first = session_manager.create_session({}, user_identity="test_user", user_token="test-token", token_claims={})
        second = session_manager.create_session({}, user_identity="test_user", user_token="test-token", token_claims={})

        first_manager = session_manager.get_session(first).data_manager
        second_manager = session_manager.get_session(second).data_manager
        assert first_manager is not second_manager

    def test_no_factory_preserves_default_construction(self):
        """An unset factory keeps today's semantics end to end."""
        session_manager = SessionManager(SessionConfig())
        session_id = session_manager.create_session(
            {}, user_identity="test_user", user_token="test-token", token_claims={}
        )

        assert isinstance(session_manager.get_session(session_id).data_manager, DataLakeDataManager)

    def test_factory_failure_propagates_without_storing_session(self):
        """A raising factory fails session creation rather than half-creating one."""

        def factory(_ctx):
            raise RuntimeError("cannot build manager")

        session_manager = SessionManager(SessionConfig(data_manager_factory=factory))

        with pytest.raises(RuntimeError, match="cannot build manager"):
            session_manager.create_session({}, user_identity="test_user", user_token="test-token", token_claims={})

        assert session_manager.storage.count() == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
