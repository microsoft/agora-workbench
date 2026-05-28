"""Tests for code validation / filesystem screening in CodeExecutionServer."""

import pytest

from .. import CodeExecutionServer
from ..auth import create_noop_auth_config
from ..code_execution_models import ServerConfig


@pytest.fixture
def server():
    """Create a minimal CodeExecutionServer for validation tests (no env build)."""
    config = ServerConfig(
        name="test",
        type="uv",
        description="Test environment",
        dependency_file="# empty",
    )
    return CodeExecutionServer(server_config=config, auth_config=create_noop_auth_config())


# =====================================================================
# Basic / empty-code validation
# =====================================================================


class TestBasicValidation:
    def test_empty_code_rejected(self, server):
        ok, msg = server.validate_code("")
        assert ok is False
        assert "empty" in msg.lower()

    def test_whitespace_only_rejected(self, server):
        ok, msg = server.validate_code("   \n  \n  ")
        assert ok is False

    def test_simple_print_accepted(self, server):
        ok, msg = server.validate_code('print("hello")')
        assert ok is True
        assert msg is None

    def test_syntax_error_passes_through(self, server):
        """Syntax errors should be left for the kernel to report."""
        ok, msg = server.validate_code("print('unclosed string")
        assert ok is True


# =====================================================================
# Blocked modules
# =====================================================================


class TestBlockedModules:
    @pytest.mark.parametrize(
        "code",
        [
            "import subprocess",
            "import shutil",
            "from subprocess import run",
            "from shutil import copytree",
            "from http.server import SimpleHTTPRequestHandler",
            "import subprocess as sp",
        ],
    )
    def test_blocked_module_rejected(self, server, code):
        ok, msg = server.validate_code(code)
        assert ok is False
        assert "not allowed" in msg.lower()

    @pytest.mark.parametrize(
        "code",
        [
            "import numpy as np",
            "import pandas as pd",
            "from pathlib import Path",
            "import os",  # os itself is allowed; only certain calls are blocked
            "import json",
        ],
    )
    def test_allowed_module_accepted(self, server, code):
        ok, msg = server.validate_code(code)
        assert ok is True


# =====================================================================
# Blocked filesystem calls
# =====================================================================


class TestBlockedFilesystemCalls:
    @pytest.mark.parametrize(
        "code",
        [
            "os.listdir('.')",
            "os.scandir('/tmp')",
            "os.walk('/tmp')",
            "os.getcwd()",
            "os.chdir('/tmp')",
            "os.system('ls')",
            "os.remove('/tmp/foo')",
            "os.unlink('/tmp/foo')",
            "os.rmdir('/tmp/foo')",
            "os.rename('/tmp/a', '/tmp/b')",
            "os.symlink('/tmp/a', '/tmp/b')",
            "os.readlink('/tmp/a')",
            "glob.glob('*')",
            "glob.iglob('*')",
            # Tainted: p traces back to pathlib.Path
            "from pathlib import Path\np = Path('/tmp')\np.iterdir()",
            "from pathlib import Path\np = Path('/tmp')\np.rglob('*')",
            "import pathlib\np = pathlib.Path('/tmp')\np.iterdir()",
            # Taint propagation: q traces back to p which traces back to Path
            "from pathlib import Path\np = Path('/tmp')\nq = p\nq.iterdir()",
        ],
    )
    def test_blocked_call_rejected(self, server, code):
        ok, msg = server.validate_code(code)
        assert ok is False
        assert "not allowed" in msg.lower()

    @pytest.mark.parametrize(
        "code",
        [
            "open('/tmp/data.csv')",
            "Path('/tmp/data.csv').read_text()",
            "df.to_csv('/tmp/output.csv')",
            "print(len(data))",
            "result = [x for x in range(10)]",
            "p.iterdir()",
            "p.rglob('*')",
        ],
    )
    def test_allowed_call_accepted(self, server, code):
        ok, msg = server.validate_code(code)
        assert ok is True

    @pytest.mark.parametrize(
        "code",
        [
            "__import__('subprocess')",
            "eval('__import__(\\'subprocess\\')')",
            "exec('print(123)')",
            "compile('print(1)', '<x>', 'exec')",
            "getattr(os, 'system')('ls')",
        ],
    )
    def test_dangerous_metaprogramming_calls_rejected(self, server, code):
        ok, msg = server.validate_code(code)
        assert ok is False
        assert "not allowed" in msg.lower()

    def test_imported_non_system_module_same_method_name_allowed(self, server):
        ok, msg = server.validate_code(
            """
import my_module
my_module.listdir('/tmp')
"""
        )
        assert ok is True

    def test_aliased_os_module_call_still_blocked(self, server):
        ok, msg = server.validate_code(
            """
import os as system_ops
system_ops.listdir('/tmp')
"""
        )
        assert ok is False
        assert "not allowed" in msg.lower()


# =====================================================================
# Absolute-path restriction
# =====================================================================


class TestPathRestriction:
    @pytest.mark.parametrize(
        "code",
        [
            "open('/etc/passwd')",
            "Path('/var/log/syslog')",
            "f = '/home/user/.ssh/id_rsa'",
            "read_file('/usr/local/bin/python')",
            "open('/mnt/data/output.nc')",
        ],
    )
    def test_disallowed_path_rejected(self, server, code):
        ok, msg = server.validate_code(code)
        assert ok is False
        assert "outside the allowed directories" in msg.lower()

    @pytest.mark.parametrize(
        "code",
        [
            "open('/tmp/data_lake_cache_abc/file.csv')",
            "path = '/tmp/results.pkl'",
        ],
    )
    def test_allowed_path_accepted(self, server, code):
        ok, msg = server.validate_code(code)
        assert ok is True

    def test_relative_path_accepted(self, server):
        ok, msg = server.validate_code("open('data.csv')")
        assert ok is True

    @pytest.mark.parametrize(
        "code",
        [
            "open('../../../../secret.txt')",
            "p = '../data/file.csv'",
            "pattern = '../../../../*'",
            "path = r'..\\..\\windows\\system32'",
        ],
    )
    def test_relative_parent_traversal_rejected(self, server, code):
        ok, msg = server.validate_code(code)
        assert ok is False
        assert "traversal" in msg.lower() or "not allowed" in msg.lower()

    def test_url_not_treated_as_path(self, server):
        """Double-slash URLs should not be flagged as paths."""
        ok, msg = server.validate_code("url = '//cdn.example.com/data.csv'")
        assert ok is True

    def test_bare_prefix_accepted(self, server):
        """The prefix itself (e.g. '/tmp') is allowed."""
        ok, msg = server.validate_code("path = '/tmp'")
        assert ok is True

    @pytest.mark.parametrize(
        "code",
        [
            "path = '/etc' + '/passwd'",
            "filename = 'passwd'\npath = f'/etc/{filename}'",
            "filename = 'passwd'\npath = '/etc/%s' % filename",
            "import os\npath = os.path.join('/etc', 'passwd')",
        ],
    )
    def test_dynamic_disallowed_path_rejected(self, server, code):
        ok, msg = server.validate_code(code)
        assert ok is False
        assert "outside the allowed directories" in msg.lower()

    @pytest.mark.parametrize(
        "code",
        [
            "filename = 'data.csv'\npath = f'/tmp/{filename}'",
            "filename = 'data.csv'\npath = '/tmp/%s' % filename",
            "import os\npath = os.path.join('/tmp', 'data.csv')",
        ],
    )
    def test_dynamic_allowed_path_accepted(self, server, code):
        ok, msg = server.validate_code(code)
        assert ok is True


# =====================================================================
# Integration: realistic multi-line code
# =====================================================================


class TestRealisticCode:
    def test_legitimate_data_analysis_accepted(self, server):
        code = """
import pandas as pd
import numpy as np

df = pd.read_csv('/tmp/grid_data.csv')
result = df.groupby('bus').agg({'p_mw': 'sum'})
result.to_csv('/tmp/aggregated.csv')
print(result)
"""
        ok, msg = server.validate_code(code)
        assert ok is True

    def test_filesystem_snooping_rejected(self, server):
        code = """
import os
for root, dirs, files in os.walk('/'):
    for f in files:
        print(os.path.join(root, f))
"""
        ok, msg = server.validate_code(code)
        assert ok is False

    def test_subprocess_shell_rejected(self, server):
        code = """
import subprocess
result = subprocess.run(['ls', '-la', '/'], capture_output=True, text=True)
print(result.stdout)
"""
        ok, msg = server.validate_code(code)
        assert ok is False

    def test_path_escape_attempt_rejected(self, server):
        code = """
from pathlib import Path
secret = Path('/etc/shadow').read_text()
print(secret)
"""
        ok, msg = server.validate_code(code)
        assert ok is False


# =====================================================================
# Subclass customisation
# =====================================================================


class TestSubclassCustomisation:
    def test_subclass_can_extend_blocked_calls(self):
        """Subclass can add more blocked calls."""

        class StricterServer(CodeExecutionServer):
            _BLOCKED_FS_CALLS = CodeExecutionServer._BLOCKED_FS_CALLS | {"open"}

        config = ServerConfig(name="strict", type="uv", description="Strict", dependency_file="# empty")
        strict = StricterServer(server_config=config, auth_config=create_noop_auth_config())

        ok, _ = strict.validate_code("open('/tmp/file.txt')")
        assert ok is False

    def test_subclass_can_add_allowed_prefixes(self):
        """Subclass can broaden allowed paths."""

        class PermissiveServer(CodeExecutionServer):
            _ALLOWED_PATH_PREFIXES = CodeExecutionServer._ALLOWED_PATH_PREFIXES + ("/mnt/data",)

        config = ServerConfig(name="permissive", type="uv", description="Permissive", dependency_file="# empty")
        permissive = PermissiveServer(server_config=config, auth_config=create_noop_auth_config())

        # /mnt/data is NOT allowed by default, but the subclass adds it
        ok, _ = permissive.validate_code("open('/mnt/data/shared/file.csv')")
        assert ok is True

        # Verify the base server still rejects /mnt/data
        base_config = ServerConfig(name="base", type="uv", description="Base", dependency_file="# empty")
        base = CodeExecutionServer(server_config=base_config, auth_config=create_noop_auth_config())
        ok, _ = base.validate_code("open('/mnt/data/shared/file.csv')")
        assert ok is False
