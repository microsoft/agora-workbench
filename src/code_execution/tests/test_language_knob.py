"""Tests for the ServerConfig.language knob and per-language kernel seams.

These exercise the small surface that makes a non-Python (R) domain possible:
the kernel-name derivation, the output/token preambles, and the validator
short-circuit. They are pure unit tests — no kernel is launched.
"""

from typing import Literal

from .. import CodeExecutionServer
from ..auth import create_noop_auth_config
from ..code_execution_models import ServerConfig
from ..sessions.manager import SessionConfig, SessionManager


def _server(language: Literal["python", "r"]) -> CodeExecutionServer:
    """A minimal server (no env build) for validator tests."""
    config = ServerConfig(
        name="test",
        type="conda",
        description="Test environment",
        dependency_file="# empty",
        language=language,
    )
    return CodeExecutionServer(server_config=config, auth_config=create_noop_auth_config())


class TestKernelName:
    def test_python_is_default(self):
        config = ServerConfig(name="t", type="uv", description="d", dependency_file="#")
        assert config.language == "python"
        assert config.get_kernel_name() == "tools-py"

    def test_r_kernel_name(self):
        config = ServerConfig(name="t", type="conda", description="d", dependency_file="#", language="r")
        assert config.get_kernel_name() == "tools-r"


class TestOutputsPreamble:
    def test_python_uses_os_environ(self):
        out = SessionManager(SessionConfig(language="python"))._prepare_outputs_preamble("s1")
        assert "import os" in out
        assert "AGORA_OUTPUT_DIR" in out

    def test_r_uses_sys_setenv(self):
        out = SessionManager(SessionConfig(language="r"))._prepare_outputs_preamble("s1")
        assert "Sys.setenv(AGORA_OUTPUT_DIR" in out
        assert "AGORA_OUTPUT_DIR <-" in out
        assert "import os" not in out


class TestTokenPreamble:
    def test_python_set_then_clear(self):
        sm = SessionManager(SessionConfig(language="python"))
        set_code = sm._prepare_code_with_token_preamble("s1", "BODY", "tok-abc")
        assert "import os" in set_code
        assert "USER_ASSERTION_TOKEN" in set_code
        assert set_code.endswith("BODY")
        clear_code = sm._prepare_code_with_token_preamble("s1", "BODY", None)
        assert "del __agora_os__.environ['USER_ASSERTION_TOKEN']" in clear_code

    def test_r_set_then_clear(self):
        sm = SessionManager(SessionConfig(language="r"))
        set_code = sm._prepare_code_with_token_preamble("s1", "BODY", "tok-abc")
        assert 'Sys.setenv(USER_ASSERTION_TOKEN = "tok-abc")' in set_code
        assert "import os" not in set_code
        assert set_code.endswith("BODY")
        clear_code = sm._prepare_code_with_token_preamble("s1", "BODY", None)
        assert 'Sys.unsetenv("USER_ASSERTION_TOKEN")' in clear_code
        assert "import os" not in clear_code

    def test_no_token_no_preamble(self):
        sm = SessionManager(SessionConfig(language="r"))
        assert sm._prepare_code_with_token_preamble("s1", "BODY", None) == "BODY"


class TestValidateShortCircuit:
    def test_python_blocks_dangerous_import(self):
        ok, _ = _server("python").validate_code("import subprocess")
        assert ok is False

    def test_r_skips_python_validation(self):
        server = _server("r")
        # Blocked under Python; for R the Python-AST validator is skipped.
        ok, _ = server.validate_code("import subprocess")
        assert ok is True
        # R-only syntax (assignment + remove) must also pass.
        ok2, _ = server.validate_code("x <- 5; remove(x)")
        assert ok2 is True
