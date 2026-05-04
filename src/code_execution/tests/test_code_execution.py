"""
Tests for code execution functionality.
"""

import asyncio
import os
from unittest.mock import patch

import pytest
from contextlib import contextmanager
from fastapi import HTTPException

from ..code_execution import CodeExecutionResult
from ..code_execution.sessions import (
    set_current_request_token,
    set_current_session,
    set_current_token_claims,
    set_current_user_identity,
)


@contextmanager
def patch_server_method(server, method_name, new_method):
    """Context manager to safely patch and restore server methods."""
    original = getattr(server, method_name)
    setattr(server, method_name, new_method)
    try:
        yield
    finally:
        setattr(server, method_name, original)


@pytest.mark.asyncio
async def test_simple_print(test_server, simple_code_samples):
    """Test basic print statement execution."""
    result = await test_server.execute_code_isolated(simple_code_samples["hello_world"], timeout=10)

    assert result.success is True
    assert "Hello, World!" in result.stdout
    assert result.stderr == ""
    assert result.error is None
    assert result.execution_time > 0


@pytest.mark.asyncio
async def test_math_calculation(test_server, simple_code_samples):
    """Test mathematical operations."""
    result = await test_server.execute_code_isolated(simple_code_samples["math_calc"], timeout=10)

    assert result.success is True
    assert "4" in result.stdout
    assert result.error is None


@pytest.mark.asyncio
async def test_numpy_import(test_server, simple_code_samples):
    """Test that environment dependencies are available."""
    result = await test_server.execute_code_isolated(simple_code_samples["import_test"], timeout=10)

    assert result.success is True
    assert result.stdout.strip()  # Should print numpy version
    assert result.error is None


@pytest.mark.asyncio
async def test_runtime_error(test_server, simple_code_samples):
    """Test that runtime errors are captured properly."""
    result = await test_server.execute_code_isolated(simple_code_samples["error_code"], timeout=10)

    assert result.success is False
    assert "ValueError" in result.stderr
    assert "Test error" in result.stderr
    assert result.error is not None


@pytest.mark.asyncio
async def test_syntax_error(test_server, simple_code_samples):
    """Test that syntax errors are captured."""
    result = await test_server.execute_code_isolated(simple_code_samples["syntax_error"], timeout=10)

    assert result.success is False
    assert "SyntaxError" in result.stderr or "unterminated" in result.stderr.lower()


@pytest.mark.asyncio
async def test_timeout(test_server, simple_code_samples):
    """Test that execution timeout works."""
    result = await test_server.execute_code_isolated(simple_code_samples["infinite_loop"], timeout=2)

    assert result.success is False
    assert "timeout" in (result.stderr or "").lower() or "timeout" in (result.error or "").lower()
    assert result.execution_time >= 2


@pytest.mark.asyncio
async def test_multiline_code(test_server, simple_code_samples):
    """Test execution of multiline code with imports."""
    result = await test_server.execute_code_isolated(simple_code_samples["multiline"], timeout=10)

    assert result.success is True
    assert "a  b" in result.stdout or "0  1  4" in result.stdout  # DataFrame output


@pytest.mark.asyncio
async def test_empty_code(test_server):
    """Test that empty code is rejected."""
    result = await test_server.execute_code_isolated("", timeout=10)

    assert result.success is False
    assert "empty" in result.error.lower()


@pytest.mark.asyncio
async def test_working_directory(test_server):
    """Test that working directory is properly set."""
    code = """
from pathlib import Path
probe = Path("wd_probe.txt")
probe.write_text("ok")
print(probe.resolve())
"""
    result = await test_server.execute_code_isolated(code, timeout=10)

    assert result.success is True
    resolved_probe_path = result.stdout.strip().split("\n")[-1]
    assert resolved_probe_path.endswith("/wd_probe.txt")
    assert test_server.working_dir is not None
    assert resolved_probe_path.startswith(str(test_server.working_dir))


@pytest.mark.asyncio
async def test_execution_isolation(test_server):
    """Test that executions are isolated from each other."""
    code1 = "x = 42\nprint(x)"
    code2 = "print(x)"  # Should fail because x is not defined

    result1 = await test_server.execute_code_isolated(code1, timeout=10)
    assert result1.success is True
    assert "42" in result1.stdout

    result2 = await test_server.execute_code_isolated(code2, timeout=10)
    assert result2.success is False
    assert "NameError" in result2.stderr


@pytest.mark.asyncio
async def test_stdout_stderr_separation(test_server):
    """Test that stdout and stderr are properly separated."""
    code = """
import sys
print("This is stdout")
print("This is stderr", file=sys.stderr)
"""
    result = await test_server.execute_code_isolated(code, timeout=10)

    assert result.success is True
    assert "This is stdout" in result.stdout
    assert "This is stderr" in result.stderr


@pytest.mark.asyncio
async def test_preprocess_hook(test_server):
    """Test that preprocess_code hook is called."""
    preprocessed = False

    def custom_preprocess(code: str) -> str:
        nonlocal preprocessed
        preprocessed = True
        return code

    with patch_server_method(test_server, "preprocess_code", custom_preprocess):
        await test_server.execute_code_isolated("print('test')", timeout=10)
        assert preprocessed is True


@pytest.mark.asyncio
async def test_postprocess_hook(test_server):
    """Test that postprocess_result hook is called."""
    postprocessed = False

    def custom_postprocess(result: CodeExecutionResult) -> CodeExecutionResult:
        nonlocal postprocessed
        postprocessed = True
        return result

    with patch_server_method(test_server, "postprocess_result", custom_postprocess):
        await test_server.execute_code_isolated("print('test')", timeout=10)
        assert postprocessed is True


@pytest.mark.asyncio
async def test_code_with_special_characters(test_server):
    """Test that code with various special characters is handled safely."""
    # Create a session through the server's session manager
    session_id = test_server.session_manager.create_session(
        data={"namespace": {"x": 10}}, user_identity="test_user", user_token="test-token", token_claims={}
    )
    session = test_server.session_manager.get_session(session_id)
    set_current_session(session)

    test_cases = [
        # Triple quotes that could break old escaping
        ('print("""Hello World""")', "Hello World"),
        # Newlines
        ('print("Line 1\\nLine 2")', "Line 1\nLine 2"),
        # Single quotes
        ("print('Single quotes')", "Single quotes"),
        # Mixed quotes
        ("""print("Double's and 'single' quotes")""", "Double's and 'single' quotes"),
        # Backslashes
        ('print("Path: C:\\\\Users\\\\test")', "Path: C:\\Users\\test"),
        # Unicode
        ('print("Hello 世界 🌍")', "Hello 世界 🌍"),
        # Code that looks like an injection attempt
        ('x = 1\nprint("value:", x)', "value: 1"),
    ]

    for code, expected_output in test_cases:
        result = await test_server.execute_code_with_session(code=code, timeout=10, session_id=session_id)
        assert result.success, f"Failed to execute: {code}. Error: {result.error}"
        assert expected_output in result.stdout, (
            f"Expected output '{expected_output}' not found in stdout: {result.stdout}"
        )


@pytest.mark.asyncio
async def test_namespace_persistence_with_special_chars(test_server):
    """Test that namespace persistence works with code containing special characters."""
    # Clear any existing sessions
    for session_info in test_server.session_manager.list_sessions():
        test_server.session_manager.close_session(session_info["session_id"])

    # Create a session
    session_id = test_server.session_manager.create_session(
        data={"namespace": {}}, user_identity="test_user", user_token="test-token", token_claims={}
    )

    # First execution: set a variable with special characters
    code1 = 'message = """Multi-line\nstring with \'quotes\'"""'
    result1 = await test_server.execute_code_with_session(code=code1, timeout=10, session_id=session_id)
    assert result1.success, f"First execution failed: {result1.error}\nStderr: {result1.stderr}"

    # Second execution: access the variable
    code2 = "print(message)"
    result2 = await test_server.execute_code_with_session(code=code2, timeout=10, session_id=session_id)
    assert result2.success, f"Second execution failed: {result2.error}\nStderr: {result2.stderr}"
    assert "Multi-line" in result2.stdout
    assert "string with 'quotes'" in result2.stdout


@pytest.mark.asyncio
async def test_background_execution_preserves_session_state(test_server):
    """Background execution should complete in-session and preserve resulting variables."""
    session_id = test_server.session_manager.create_session(
        data={"namespace": {}}, user_identity="test_user", user_token="test-token", token_claims={}
    )
    session = test_server.session_manager.get_session(session_id)
    set_current_session(session)
    try:
        started = await test_server._execute_code_background(
            "import time\ntime.sleep(0.2)\nbg_value = 99\nprint('background done')",
            timeout=10,
        )
    finally:
        set_current_session(None)

    assert started["job_id"].startswith("j_")
    assert started["status"] == "running"
    assert started["session_id"] == session_id

    final_status = None
    for _ in range(30):
        status = test_server.session_manager.check_background_job(started["job_id"])
        if status["status"] != "running":
            final_status = status
            break
        await asyncio.sleep(0.1)

    assert final_status is not None
    assert final_status["status"] == "completed"
    assert final_status["success"] is True
    assert "background done" in final_status["stdout"]

    follow_up = await test_server.execute_code_with_session(code="print(bg_value)", timeout=10, session_id=session_id)
    assert follow_up.success is True
    assert "99" in follow_up.stdout


@pytest.mark.asyncio
async def test_get_or_create_session_returns_typed_429_when_at_capacity(test_server):
    """Session creation at capacity should surface as a typed 429 error."""
    sessions_to_close = []
    original_max_sessions = test_server.session_manager.config.max_sessions
    try:
        existing_sessions = test_server.session_manager.storage.count()
        test_server.session_manager.config.max_sessions = existing_sessions + 1
        sessions_to_close.append(
            test_server.session_manager.create_session(
                data={},
                user_identity="test_user",
                user_token="test-token",
                token_claims={},
            )
        )

        set_current_session(None)
        set_current_user_identity("test_user")
        set_current_request_token("test-token")
        set_current_token_claims({"oid": "test_user"})

        with pytest.raises(HTTPException) as exc_info:
            await test_server._get_or_create_session("test_tool")

        assert exc_info.value.status_code == 429
        detail = exc_info.value.detail
        assert isinstance(detail, dict)
        assert detail["error_type"] == "max_sessions_reached"  # type: ignore[index]
    finally:
        set_current_session(None)
        set_current_user_identity(None)
        set_current_request_token(None)
        set_current_token_claims(None)
        test_server.session_manager.config.max_sessions = original_max_sessions
        for session_id in sessions_to_close:
            test_server.session_manager.close_session(session_id)


# ============================================================================
# Output truncation tests
# ============================================================================


class TestOutputTruncation:
    """Unit tests for _truncate_output_if_needed."""

    def _make_result(self, stdout: str = "", stderr: str = "") -> CodeExecutionResult:
        return CodeExecutionResult(stdout=stdout, stderr=stderr, success=True)

    def test_no_truncation_when_within_threshold(self, test_server):
        """Output within the threshold is returned unchanged."""
        original_threshold = test_server.output_truncation_threshold
        test_server.output_truncation_threshold = 100
        try:
            result = self._make_result(stdout="x" * 99, stderr="y" * 50)
            out = test_server._truncate_output_if_needed(result)
            assert out.stdout == result.stdout
            assert out.stderr == result.stderr
        finally:
            test_server.output_truncation_threshold = original_threshold

    def test_stdout_truncated_at_threshold(self, test_server):
        """stdout exceeding the threshold is trimmed and a notice is appended."""
        original_threshold = test_server.output_truncation_threshold
        test_server.output_truncation_threshold = 50
        try:
            long_output = "a" * 200
            result = self._make_result(stdout=long_output)
            out = test_server._truncate_output_if_needed(result)
            assert out.stdout.startswith("a" * 50)
            assert "OUTPUT TRUNCATED" in out.stdout
            assert "200" in out.stdout  # original length mentioned
            assert "50" in out.stdout  # threshold mentioned
            # The kept prefix must not exceed the threshold
            kept = out.stdout[: out.stdout.index("\n[OUTPUT TRUNCATED")]
            assert len(kept) == 50
        finally:
            test_server.output_truncation_threshold = original_threshold

    def test_stderr_truncated_at_threshold(self, test_server):
        """stderr exceeding the threshold keeps head + tail with a notice."""
        original_threshold = test_server.output_truncation_threshold
        test_server.output_truncation_threshold = 50
        try:
            long_error = "e" * 200
            result = self._make_result(stderr=long_error)
            out = test_server._truncate_output_if_needed(result)
            assert "STDERR TRUNCATED" in out.stderr
            # Head (first 25 chars) and tail (last 25 chars) are both preserved
            assert out.stderr.startswith("e" * 25)
            assert out.stderr.endswith("e" * 25)
        finally:
            test_server.output_truncation_threshold = original_threshold

    def test_both_streams_truncated_independently(self, test_server):
        """Both stdout and stderr can be truncated in the same result."""
        original_threshold = test_server.output_truncation_threshold
        test_server.output_truncation_threshold = 30
        try:
            result = self._make_result(stdout="o" * 100, stderr="e" * 100)
            out = test_server._truncate_output_if_needed(result)
            assert "OUTPUT TRUNCATED" in out.stdout
            assert "STDERR TRUNCATED" in out.stderr
        finally:
            test_server.output_truncation_threshold = original_threshold

    def test_truncation_disabled_when_threshold_zero(self, test_server):
        """Setting threshold to 0 disables all truncation."""
        original_threshold = test_server.output_truncation_threshold
        test_server.output_truncation_threshold = 0
        try:
            big = "x" * 1_000_000
            result = self._make_result(stdout=big, stderr=big)
            out = test_server._truncate_output_if_needed(result)
            assert out.stdout == big
            assert out.stderr == big
        finally:
            test_server.output_truncation_threshold = original_threshold

    def test_truncation_guidance_mentions_server_side_inspection(self, test_server):
        """The truncation notice guides the LLM to investigate server-side."""
        original_threshold = test_server.output_truncation_threshold
        test_server.output_truncation_threshold = 10
        try:
            result = self._make_result(stdout="x" * 100)
            out = test_server._truncate_output_if_needed(result)
            notice = out.stdout[10:]
            # Must mention server-side inspection rather than pulling data
            assert "server-side" in notice or "MCP interface" in notice
        finally:
            test_server.output_truncation_threshold = original_threshold

    @pytest.mark.asyncio
    async def test_execute_code_truncates_large_stdout(self, test_server):
        """Truncation applies to large stdout from code execution."""
        original_threshold = test_server.output_truncation_threshold
        test_server.output_truncation_threshold = 100
        try:
            # Generate output well above the threshold
            code = "print('x' * 500)"
            raw_result = await test_server.execute_code_isolated(code, timeout=10)
            assert raw_result.success
            # Truncation is applied at the public boundary (_execute_code_with_tracing),
            # not inside _execute_code, so call it explicitly here.
            result = test_server._truncate_output_if_needed(raw_result)
            assert "OUTPUT TRUNCATED" in result.stdout
            # The kept prefix should be at most threshold characters
            kept = result.stdout[: result.stdout.index("[OUTPUT TRUNCATED")]
            assert len(kept.rstrip()) <= 100
        finally:
            test_server.output_truncation_threshold = original_threshold

    def test_stderr_preserves_tail_for_tracebacks(self, test_server):
        """stderr truncation keeps head + tail so traceback endings are visible."""
        original_threshold = test_server.output_truncation_threshold
        test_server.output_truncation_threshold = 100
        try:
            # Simulate a long stderr where the traceback message is at the end
            padding = "x" * 300
            traceback_tail = "ValueError: something went wrong\n"
            stderr = padding + traceback_tail
            result = self._make_result(stderr=stderr)
            out = test_server._truncate_output_if_needed(result)
            assert "STDERR TRUNCATED" in out.stderr
            # The tail of the original stderr (traceback) should be preserved
            assert out.stderr.endswith(traceback_tail)
            # The head of the original stderr should be preserved
            assert out.stderr.startswith("x" * 50)
        finally:
            test_server.output_truncation_threshold = original_threshold


# ============================================================================
# Environment variable parsing tests
# ============================================================================


class TestEnvVarParsing:
    """Tests for CODE_OUTPUT_TRUNCATION_THRESHOLD env var parsing."""

    def test_env_var_overrides_constructor_default(self, test_server):
        """Env var takes precedence over the constructor default."""
        with patch.dict(os.environ, {"CODE_OUTPUT_TRUNCATION_THRESHOLD": "12345"}):
            test_server.__class__.__init__(
                test_server,
                environment_config=test_server.environment_config,
                auth_config=test_server.auth_config,
                output_truncation_threshold=99999,
            )
            assert test_server.output_truncation_threshold == 12345

    def test_env_var_with_underscores(self, test_server):
        """Underscored numeric values (e.g. '50_000') are accepted."""
        with patch.dict(os.environ, {"CODE_OUTPUT_TRUNCATION_THRESHOLD": "50_000"}):
            test_server.__class__.__init__(
                test_server,
                environment_config=test_server.environment_config,
                auth_config=test_server.auth_config,
            )
            assert test_server.output_truncation_threshold == 50000

    def test_env_var_with_whitespace(self, test_server):
        """Leading/trailing whitespace in the env var is stripped."""
        with patch.dict(os.environ, {"CODE_OUTPUT_TRUNCATION_THRESHOLD": "  1000  "}):
            test_server.__class__.__init__(
                test_server,
                environment_config=test_server.environment_config,
                auth_config=test_server.auth_config,
            )
            assert test_server.output_truncation_threshold == 1000

    def test_env_var_invalid_falls_back_to_default(self, test_server):
        """An unparseable env var falls back to the constructor default."""
        with patch.dict(os.environ, {"CODE_OUTPUT_TRUNCATION_THRESHOLD": "not_a_number"}):
            test_server.__class__.__init__(
                test_server,
                environment_config=test_server.environment_config,
                auth_config=test_server.auth_config,
                output_truncation_threshold=42000,
            )
            assert test_server.output_truncation_threshold == 42000

    def test_env_var_negative_falls_back_to_default(self, test_server):
        """A negative env var value falls back to the constructor default."""
        with patch.dict(os.environ, {"CODE_OUTPUT_TRUNCATION_THRESHOLD": "-1"}):
            test_server.__class__.__init__(
                test_server,
                environment_config=test_server.environment_config,
                auth_config=test_server.auth_config,
                output_truncation_threshold=42000,
            )
            assert test_server.output_truncation_threshold == 42000

    def test_env_var_zero_disables_truncation(self, test_server):
        """Setting the env var to '0' disables truncation."""
        with patch.dict(os.environ, {"CODE_OUTPUT_TRUNCATION_THRESHOLD": "0"}):
            test_server.__class__.__init__(
                test_server,
                environment_config=test_server.environment_config,
                auth_config=test_server.auth_config,
            )
            assert test_server.output_truncation_threshold == 0

    def test_no_env_var_uses_constructor_default(self, test_server):
        """Without the env var, the constructor default is used."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CODE_OUTPUT_TRUNCATION_THRESHOLD", None)
            test_server.__class__.__init__(
                test_server,
                environment_config=test_server.environment_config,
                auth_config=test_server.auth_config,
                output_truncation_threshold=77777,
            )
            assert test_server.output_truncation_threshold == 77777
