"""Tests for code validation / filesystem screening in CodeExecutionServer."""

import pytest

from .. import CodeExecutionServer, code_execution
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
            "from os import path as eval\neval('value')",
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

    @pytest.mark.parametrize("name", ["walk", "remove", "replace"])
    @pytest.mark.parametrize(
        "definition",
        [
            "def {name}(obj):\n    return [obj]",
            "async def {name}(obj):\n    return [obj]",
            "{name} = lambda obj: [obj]",
        ],
    )
    def test_local_filesystem_named_helper_allowed(self, server, name, definition):
        code = definition.format(name=name) + f"\n{name}(1)"
        assert server.validate_code(code) == (True, None)

    def test_nested_recursive_walk_helper_allowed(self, server):
        assert server.validate_code(
            """
def collect(obj):
    def walk(value, prefix=''):
        if isinstance(value, dict):
            return [walk(item, key) for key, item in value.items()]
        return [(prefix, value)]
    return walk(obj)
collect({'cost': 1})
"""
        ) == (True, None)

    @pytest.mark.parametrize(
        "code",
        [
            "'a'.replace('a', 'b')",
        ],
    )
    def test_non_filesystem_method_call_allowed(self, server, code):
        assert server.validate_code(code) == (True, None)

    @pytest.mark.parametrize(
        "code",
        [
            "from os import walk\nwalk('/tmp')",
            "from os import remove\nremove('/tmp/file')",
            "from os import replace\nreplace('/tmp/a', '/tmp/b')",
            "from os import walk as scan\nscan('/tmp')",
            "from glob import glob as find\nfind('*')",
            "from glob import iglob\niglob('*')",
            "from os import *\nwalk('/tmp')",
            "from glob import *\nglob('*')",
            "import os\nwalk = os.walk\nwalk('/tmp')",
            "def walk(obj):\n    return [obj]\nfrom os import walk\nwalk('/tmp')",
        ],
    )
    def test_filesystem_function_binding_rejected(self, server, code):
        ok, msg = server.validate_code(code)
        assert ok is False
        assert "not allowed" in msg.lower()

    @pytest.mark.parametrize(
        "code",
        [
            "from os import remove\nremove('/tmp/file')\nfrom operator import add as remove",
            "from os import remove\nremove('/tmp/file')\nfrom os import getenv as remove",
            "from os import walk as scan\nscan('/tmp')\nfrom glob import escape as scan",
            "import os\nwalk: object = os.walk\nwalk('/tmp')",
            "import os\nwalk = scan = os.walk\nwalk('/tmp')",
            "import os\nwalk = scan = os.walk\nscan('/tmp')",
            "import os\nwalk, scan = os.walk, os.scandir\nwalk('/tmp')",
            "import os\nwalk = (scan := os.walk)\nwalk('/tmp')",
            "import os\n(scan := os.walk)\nscan('/tmp')",
            "from os.path import join as remove\nimport os\nremove = os.remove\nremove('/tmp/file')",
            "from os.path import join as scan\nimport os\nscan = os.remove\nscan('/tmp/file')",
            "import os\nscan = os.walk\nscan = lambda obj: [obj]\nscan('/tmp')",
            "import os\nscan = os.walk\nscan = os.scandir\nscan('/tmp')",
            "import os as source\nsource.walk('/tmp')\nimport glob as source",
            "import os\nimport glob\nsource = os\nsource = glob\nsource.walk('/tmp')",
            "import os\nfirst = os.walk\nsecond = first\nthird = second\nfourth = third\nfourth('/tmp')",
        ],
    )
    def test_filesystem_binding_provenance_preserved(self, server, code):
        ok, msg = server.validate_code(code)
        assert ok is False
        assert "not allowed" in msg.lower()

    @pytest.mark.parametrize(
        "code",
        [
            "from glob import os\nos.system('echo example')",
            "from glob import os as operations\noperations.remove('/tmp/file')",
            "from pathlib import os as operations\noperations.walk('/tmp')",
            "from glob import os as source\nwalk = source.walk\nwalk('/tmp')",
            "from random import _os as os\nos.system('echo example')",
            "import glob\noperations = glob.os\noperations.system('echo example')",
        ],
    )
    def test_filesystem_module_reexport_rejected(self, server, code):
        ok, msg = server.validate_code(code)
        assert ok is False
        assert "not allowed" in msg.lower()

    @pytest.mark.parametrize(
        "code",
        [
            "import os\nsystem = os.system if True else None\nsystem('echo example')",
            "import os\nwalk = os.walk if enabled else helper\nwalk('/tmp')",
            "import os\nscan = helper if enabled else os.walk\nscan('/tmp')",
            "import os\nremove = enabled and os.remove\nremove('/tmp/file')",
            "import os\nreplace = helper or os.replace\nreplace('/tmp/a', '/tmp/b')",
            "import os\nscan = (os.walk if enabled else helper) or fallback\nscan('/tmp')",
        ],
    )
    def test_conditional_filesystem_binding_rejected(self, server, code):
        ok, msg = server.validate_code(code)
        assert ok is False
        assert "not allowed" in msg.lower()

    @pytest.mark.parametrize(
        "code",
        [
            "walk = (lambda obj: [obj]) if enabled else (lambda obj: obj)\nwalk(1)",
            "walk = (lambda obj: [obj]) or (lambda obj: obj)\nwalk(1)",
            "import my_module as os\nos.listdir('/tmp')",
        ],
    )
    def test_conditional_helpers_and_non_system_imports_allowed(self, server, code):
        assert server.validate_code(code) == (True, None)

    @pytest.mark.parametrize(
        "code",
        [
            "from posix import remove\nremove('/tmp/file')",
            "from nt import remove\nremove('/tmp/file')",
            "from posix import remove as erase\nerase('/tmp/file')",
            "import posix as native\nnative.remove('/tmp/file')",
            "from nt import *\nremove('/tmp/file')",
        ],
    )
    def test_native_filesystem_import_rejected(self, server, code):
        ok, msg = server.validate_code(code)
        assert ok is False
        assert "not allowed" in msg.lower()

    @pytest.mark.parametrize(
        "code",
        [
            "import os\nfor remove in [os.remove]:\n    remove('/tmp/file')",
            "import os\n[remove('/tmp/file') for remove in [os.remove]]",
            "import os\nlist(remove('/tmp/file') for remove in (os.remove,))",
            "import os\ndef run(remove=os.remove):\n    remove('/tmp/file')\nrun()",
            "import os\ndef run(*, erase=os.remove):\n    erase('/tmp/file')\nrun()",
            "import os\nrun = lambda erase=os.remove: erase('/tmp/file')\nrun()",
            "import os\nasync def run(remove=os.remove):\n    remove('/tmp/file')",
            "import os\nasync def run():\n    async for remove in operations:\n        remove('/tmp/file')\noperations = [os.remove]",
        ],
    )
    def test_loop_and_default_callable_binding_rejected(self, server, code):
        ok, msg = server.validate_code(code)
        assert ok is False
        assert "not allowed" in msg.lower()

    @pytest.mark.parametrize(
        "code",
        [
            "from pathlib import Path\np = Path('/tmp')\nfind = p.glob\nfind('*.csv')",
            "from pathlib import Path\np = Path('/tmp')\nmove = p.replace\nmove('/tmp/file')",
        ],
    )
    def test_allowed_pathlib_method_alias_accepted(self, server, code):
        assert server.validate_code(code) == (True, None)

    @pytest.mark.parametrize(
        "code",
        [
            "from pathlib import Path\np = Path('/tmp')\nscan = p.rglob\nscan('*')",
            "from pathlib import Path\np = Path('/tmp')\nscan = p.iterdir\nscan()",
            "import glob\nscan = glob.glob\nscan('*')",
            "import os\nmove = os.replace\nmove('/tmp/a', '/tmp/b')",
            "import glob\nfrom pathlib import Path\np = Path('/tmp')\nscan = p.glob\nscan = glob.glob\nscan('*')",
        ],
    )
    def test_blocked_method_alias_retains_family(self, server, code):
        ok, msg = server.validate_code(code)
        assert ok is False
        assert "not allowed" in msg.lower()

    @pytest.mark.parametrize(
        "code",
        [
            "import os\ncollect, scan = (lambda obj: [obj]), os.walk\ncollect(1)",
            "import os\n[collect, scan] = [(lambda obj: [obj]), os.walk]\ncollect(1)",
            "import os\ncollect, (scan, value) = (lambda obj: [obj]), (os.walk, 1)\ncollect(1)",
            "import os\ncollect, *scans = (lambda obj: [obj]), os.walk, os.scandir\ncollect(1)",
            "import os\n*scans, collect = os.walk, os.scandir, (lambda obj: [obj])\ncollect(1)",
            "import os\nfor collect, scan in [((lambda obj: [obj]), os.walk)]:\n    collect(1)",
            "def walk(obj):\n    return [obj]\nfrom posix import *\nwalk(1)",
        ],
    )
    def test_unrelated_helpers_do_not_inherit_filesystem_sources(self, server, code):
        assert server.validate_code(code) == (True, None)

    @pytest.mark.parametrize(
        "code",
        [
            "walk(1)",
            "system('echo example')",
            "listdir('.')",
            "listdir = listdir\nlistdir('.')",
            "from my_module import walk\nwalk(1)",
            "walk(1)\ndef walk(obj):\n    return [obj]",
            "if enabled:\n    def walk(obj):\n        return [obj]\nwalk(1)",
            "def outer():\n    def walk(obj):\n        return [obj]\n    return walk(1)\nwalk(1)",
            "class Helpers:\n    def walk(obj):\n        return [obj]\nwalk(1)",
            "def walk(obj):\n    return [obj]\nwalk = unknown\nwalk(1)",
            "@unknown\ndef walk(obj):\n    return [obj]\nwalk(1)",
            "def walk(obj):\n    return [obj]\ndef run():\n    walk(1)\n    walk = lambda obj: [obj]",
            "def walk(obj):\n    return [obj]\n[walk(1) for walk in helpers]",
            "def run(remove=lambda obj: [obj]):\n    return remove(1)\nrun()",
            "import os\ndef run(walk):\n    return walk('/tmp')\nrun(os.walk)",
            "def walk(obj):\n    return [obj]\ndef mutate():\n    global walk\n    walk = unknown\nmutate()\nwalk(1)",
            "walk = lambda obj=walk(1): [obj]\nwalk(1)",
            "def run():\n    return walk(1)\nrun()\ndef walk(obj):\n    return [obj]",
            "def walk(obj):\n    return [obj]\ndef run():\n    return walk(1)\nwalk = unknown\nrun()",
            "def walk(obj):\n    return [obj]\n[(walk := unknown) for obj in values]\nwalk(1)",
            "if enabled:\n    def walk(obj):\n        return [obj]\nelse:\n    walk(1)",
        ],
    )
    def test_unproven_or_ambiguous_helper_rejected(self, server, code):
        ok, msg = server.validate_code(code)
        assert ok is False
        assert "not allowed" in msg.lower()

    @pytest.mark.parametrize(
        "code",
        [
            "def walk(obj):\n    return [obj]\ndef run():\n    return walk(1)\nrun()",
            "walk: object = lambda obj: [obj]\nwalk(1)",
            "walk = lambda obj: walk(obj - 1) if obj else []\nwalk(1)",
            "def walk(obj):\n    return [obj]\n[walk(obj) for obj in values]",
            "walk, other = (lambda obj: [obj]), 1\nwalk(1)",
            "def walk(obj):\n    return [obj]\n[walk for walk in values]\nwalk(1)",
            "if enabled:\n    def walk(obj):\n        return [obj]\n    walk(1)",
            "def run():\n    if enabled:\n        walk = lambda obj: [obj]\n        return walk(1)\nrun()",
        ],
    )
    def test_scope_matched_local_helper_allowed(self, server, code):
        assert server.validate_code(code) == (True, None)

    def test_prior_cell_filesystem_import_does_not_exempt_bare_call(self, server):
        assert server.validate_code("from os import system, listdir") == (True, None)
        ok, msg = server.validate_code("system('echo example')")
        assert ok is False
        assert "not allowed" in msg.lower()

    @pytest.mark.parametrize(
        "code",
        [
            "remove('/tmp/file')\nfrom os.path import join as remove",
            "from os.path import join as remove\nremove = unknown\nremove('/tmp/file')",
            "from os.path import join as remove\ndef run(remove):\n    remove('/tmp/file')\nrun(unknown)",
            "from os.path import join as remove\nremove('a', 'b')",
            "from pathlib import Path\np = Path('/tmp')\nwalk = p.glob\nwalk('*.csv')",
        ],
    )
    def test_alias_resolution_cannot_remove_bare_name_restriction(self, server, code):
        ok, msg = server.validate_code(code)
        assert ok is False
        assert "not allowed" in msg.lower()

    @pytest.mark.parametrize(
        "code",
        [
            "import os\ndef scan():\n    return 1\ndef unrelated():\n    scan = os.walk\nscan()",
            "import os\ndef run():\n    scan = lambda: 1\n    return scan()\ndef unrelated():\n    scan = os.walk\nrun()",
        ],
    )
    def test_unrelated_scope_cannot_taint_proven_local_callable(self, server, code):
        assert server.validate_code(code) == (True, None)

    @pytest.mark.parametrize(
        "code",
        [
            "def remove(value=(lambda: remove('/tmp/file'))()):\n    return value",
            "remove, result = (lambda value: value), (lambda: remove('/tmp/file'))()",
            "remove = (lambda value: value) if (lambda: remove('/tmp/file'))() else (lambda value: value)",
            "remove = lambda value=(lambda: remove('/tmp/file'))(): value",
        ],
    )
    def test_eager_sibling_lambda_does_not_establish_recursion(self, server, code):
        ok, msg = server.validate_code(code)
        assert ok is False
        assert "not allowed" in msg.lower()

    def test_nested_lambda_in_actual_helper_body_can_recurse(self, server):
        assert server.validate_code(
            "remove = lambda value: (lambda: remove(value - 1))() if value else 0\nremove(1)"
        ) == (True, None)

    @pytest.mark.parametrize(
        "code",
        [
            "import os\nslots = {}\nslots[(remove := os.remove)] = lambda value: value\nremove('/tmp/file')",
            "import os\nslots = {}\nremove, slots[(remove := os.remove)] = (lambda value: value), (lambda value: value)\nremove('/tmp/file')",
            "import os\n(walk := os.walk).tag = lambda value: value\nwalk('/tmp')",
        ],
    )
    def test_target_expression_does_not_receive_assignment_value(self, server, code):
        ok, msg = server.validate_code(code)
        assert ok is False
        assert "not allowed" in msg.lower()

    def test_subscript_expression_does_not_inherit_rhs_filesystem_source(self, server):
        assert server.validate_code(
            "import os\nslots = {}\nslots[(collect := lambda value: value)] = os.walk\ncollect(1)"
        ) == (True, None)

    @pytest.mark.parametrize(
        "code",
        [
            "def unrelated(Path=None):\n    pass\np = Path('/tmp')\np.iterdir()",
            "def unrelated(*, Path=None):\n    pass\np = Path('/tmp')\np.rglob('*')",
            "import os\ndef unrelated(Path=os.getenv):\n    pass\np = Path('/tmp')\np.iterdir()",
        ],
    )
    def test_unrelated_binding_cannot_erase_known_constructor(self, server, code):
        assert server.validate_code("from pathlib import Path") == (True, None)
        ok, msg = server.validate_code(code)
        assert ok is False
        assert "not allowed" in msg.lower()

    @pytest.mark.parametrize("blocked_call", [False, True])
    def test_reverse_alias_propagation_work_is_bounded(self, server, monkeypatch, blocked_call):
        length = 256
        code = (
            "import os\nif False:\n"
            + "\n".join(f"    alias{i} = alias{i + 1}" for i in range(length))
            + f"\n    alias{length} = os.path"
        )
        if blocked_call:
            code += "\nalias0.listdir('/tmp')"
        resolve = code_execution._resolve_rhs_sources
        resolutions = 0

        def counted_resolve(node, tainted):
            nonlocal resolutions
            resolutions += 1
            return resolve(node, tainted)

        monkeypatch.setattr(code_execution, "_resolve_rhs_sources", counted_resolve)
        ok, msg = server.validate_code(code)
        assert ok is not blocked_call, msg
        assert resolutions <= 4 * (length + 1)

    def test_cyclic_alias_propagation_retains_filesystem_source(self, server):
        ok, msg = server.validate_code(
            "import os\nfirst = second\nsecond = first\nsecond = os.path\nfirst.listdir('/tmp')"
        )
        assert ok is False
        assert "not allowed" in msg.lower()

    @pytest.mark.parametrize(
        "code",
        [
            "import os\ncollect, scan = (lambda obj: [obj]), os.walk\nscan('/tmp')",
            "import os\ncollect, (scan, value) = (lambda obj: [obj]), (os.walk, 1)\nscan('/tmp')",
            "import os\nfor collect, scan in [((lambda obj: [obj]), os.walk)]:\n    scan('/tmp')",
            "import os\noperations = ((lambda obj: [obj]), os.walk)\ncollect, scan = operations\nscan('/tmp')",
        ],
    )
    def test_unpacked_filesystem_binding_rejected(self, server, code):
        ok, msg = server.validate_code(code)
        assert ok is False
        assert "not allowed" in msg.lower()

    @pytest.mark.parametrize("name", ["iterdir", "rglob"])
    def test_pathlib_wildcard_does_not_shadow_local_method_named_helper(self, server, name):
        code = f"def {name}(obj):\n    return [obj]\nfrom pathlib import *\n{name}(1)"
        assert server.validate_code(code) == (True, None)

    @pytest.mark.parametrize(
        "code",
        [
            "from pathlib import *\np = Path('/tmp')\np.iterdir()",
            "from pathlib import *\nConstructor = Path\np = Constructor('/tmp')\np.rglob('*')",
            "from pathlib import Path as Constructor\np: object = Constructor('/tmp')\np.iterdir()",
        ],
    )
    def test_pathlib_constructor_bindings_traced(self, server, code):
        ok, msg = server.validate_code(code)
        assert ok is False
        assert "not allowed" in msg.lower()

    @pytest.mark.parametrize(
        "code",
        [
            "def walk(obj):\n    return os.walk(obj)\nwalk('/tmp')",
            "walk = lambda obj: os.walk(obj)\nwalk('/tmp')",
            "def walk(obj):\n    return [obj]\nwalk('/etc/passwd')",
        ],
    )
    def test_local_helper_does_not_bypass_other_validation(self, server, code):
        ok, msg = server.validate_code(code)
        assert ok is False
        assert msg is not None


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


class TestFStringSlashNotAbsolutePath:
    """An interior '/' between f-string placeholders is a display separator,
    not an absolute path.  Regression for the false positive where
    f"{done}/{total}" (and similar) was rejected as Absolute path '/'."""

    @pytest.mark.parametrize(
        "code",
        [
            'msg = f"coverage: {int(valid_xy.sum())}/{len(wa)}"',
            'msg = f"{done}/{total}"',
            'msg = f"rows {n}/{m} ok"',
            'label = f"{a}/{b}/{c}"',
            'ratio = f"{num}/{den}"',
            'padded = f"{value:/>5}"',
            # ".." that only appears because of an adjacent placeholder
            # (e.g. a numeric range) must likewise not trip traversal.
            'rng = f"{lo}..{hi}"',
        ],
    )
    def test_fstring_interior_slash_accepted(self, server, code):
        ok, msg = server.validate_code(code)
        assert ok is True, msg

    @pytest.mark.parametrize(
        "code",
        [
            # Genuine dynamic absolute paths must STILL be rejected: the
            # f-string, reconstructed as a whole, is rooted at '/'.
            'user = "x"\np = f"/etc/{user}/secrets"',
            'name = "x"\np = f"/{name}"',
            'p = f"/var/log/{name}.log"',
            # Nested f-string that resolves to an absolute path.
            "name = 'x'\np = f\"{f'/etc/{name}'}\"",
        ],
    )
    def test_fstring_genuine_absolute_path_still_rejected(self, server, code):
        ok, msg = server.validate_code(code)
        assert ok is False
        assert "outside the allowed directories" in msg.lower()

    def test_fstring_traversal_still_rejected(self, server):
        ok, msg = server.validate_code('base = "x"\np = f"{base}/../etc/passwd"')
        assert ok is False
        assert "traversal" in msg.lower() or "not allowed" in msg.lower()

    def test_fstring_allowed_prefix_accepted(self, server):
        ok, msg = server.validate_code('name = "x"\np = f"/tmp/{name}.csv"')
        assert ok is True, msg


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
    def test_custom_dangerous_call_not_exempted_by_alias_family(self, server):
        server._BLOCKED_DANGEROUS_CALLS = server._BLOCKED_DANGEROUS_CALLS | {"glob"}
        ok, msg = server.validate_code("from pathlib import Path\np = Path('/tmp')\nfind = p.glob\nfind('*.csv')")
        assert ok is False
        assert "not allowed" in msg.lower()

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
