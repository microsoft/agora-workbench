"""Unit tests for the Linux kernel network-namespace launcher."""

import io
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from jupyter_client.manager import AsyncKernelManager

from ..sessions import network_isolation


def _create_seccomp_venv(tmp_path, *, include_system_site_packages: bool = False):
    base_root = tmp_path / "base"
    (base_root / "bin").mkdir(parents=True)
    standard_library = base_root / "lib" / "python3.11"
    (standard_library / "encodings").mkdir(parents=True)
    (standard_library / "encodings" / "__init__.py").touch()
    base_site_packages = standard_library / "site-packages"
    (base_site_packages / "base_only").mkdir(parents=True)

    venv_root = tmp_path / "venv"
    executable = venv_root / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.touch()
    (venv_root / "pyvenv.cfg").write_text(
        "\n".join(
            [
                f"home = {base_root / 'bin'}",
                f"include-system-site-packages = {str(include_system_site_packages).lower()}",
                "version = 3.11.9",
            ]
        ),
        encoding="utf-8",
    )
    venv_site_packages = venv_root / "lib" / "python3.11" / "site-packages"
    (venv_site_packages / "ipykernel").mkdir(parents=True)
    (venv_site_packages / "ipykernel" / "__init__.py").touch()
    return executable, base_site_packages, venv_site_packages


def test_isolation_prefix_maps_non_root_caller_to_namespace_root():
    executables = {
        "unshare": "/usr/bin/unshare",
        "setpriv": "/usr/bin/setpriv",
        "ip": "/usr/sbin/ip",
    }

    with patch(
        "agora_workbench.code_execution.sessions.network_isolation.shutil.which",
        side_effect=executables.get,
    ):
        prefix = network_isolation._isolation_prefix()

    assert "--map-root-user" in prefix
    assert "--map-current-user" not in prefix


def test_namespace_launcher_enables_private_loopback_and_drops_capabilities():
    executables = {
        "ip": "/usr/sbin/ip",
        "setpriv": "/usr/bin/setpriv",
    }

    with (
        patch(
            "agora_workbench.code_execution.sessions.network_isolation.shutil.which",
            side_effect=executables.get,
        ),
        patch("agora_workbench.code_execution.sessions.network_isolation.subprocess.run") as run,
        patch(
            "agora_workbench.code_execution.sessions.network_isolation.os.execv",
            side_effect=RuntimeError("exec intercepted"),
        ) as execv,
        pytest.raises(RuntimeError, match="exec intercepted"),
    ):
        network_isolation._namespace_launcher(
            ["/env/bin/python", "-m", "ipykernel_launcher"],
            ip="/usr/sbin/ip",
            setpriv="/usr/bin/setpriv",
        )

    run.assert_called_once_with(
        ["/usr/sbin/ip", "link", "set", "lo", "up"],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    execv.assert_called_once_with(
        "/usr/bin/setpriv",
        [
            "/usr/bin/setpriv",
            "--no-new-privs",
            "--bounding-set=-all",
            "--inh-caps=-all",
            "--ambient-caps=-all",
            "--",
            "/env/bin/python",
            "-m",
            "ipykernel_launcher",
        ],
    )


def test_seccomp_filter_allows_unix_sockets_and_denies_ip_sockets():
    try:
        network_isolation.validate_seccomp_isolation_support()
    except RuntimeError as exc:
        pytest.skip(str(exc))


def test_seccomp_validation_reports_probe_failure():
    with (
        patch(
            "agora_workbench.code_execution.sessions.network_isolation.subprocess.run",
            return_value=network_isolation.subprocess.CompletedProcess([], 1, "", "filter rejected"),
        ),
        pytest.raises(RuntimeError, match="seccomp kernel networking is unavailable: filter rejected"),
    ):
        network_isolation.validate_seccomp_isolation_support()


def test_seccomp_filter_rejects_unresolved_io_uring_syscall():
    class FakeFunction:
        def __init__(self, implementation):
            self.implementation = implementation
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):
            return self.implementation(*args)

    class FakeSeccomp:
        def __init__(self):
            syscall_numbers = {
                b"socket": 1,
                b"socketpair": 2,
                b"connect": 3,
                b"socketcall": -1,
                b"io_uring_setup": -1,
            }
            self.seccomp_init = FakeFunction(lambda _action: 1)
            self.seccomp_release = FakeFunction(lambda _context: None)
            self.seccomp_attr_set = FakeFunction(lambda _context, _attribute, _value: 0)
            self.seccomp_syscall_resolve_name = FakeFunction(syscall_numbers.__getitem__)
            self.seccomp_rule_add_array = FakeFunction(
                lambda _context, _action, _syscall, _argument_count, _comparisons: 0
            )
            self.seccomp_load = FakeFunction(lambda _context: 0)

    class FakeLibc:
        prctl = FakeFunction(lambda *_args: 0)

    fake_seccomp = FakeSeccomp()

    def fake_cdll(name, **_kwargs):
        return FakeLibc() if name is None else fake_seccomp

    capabilities = "CapInh:\t0\nCapPrm:\t0\nCapEff:\t0\nCapAmb:\t0\n"
    with (
        patch("ctypes.CDLL", side_effect=fake_cdll),
        patch("builtins.open", side_effect=lambda *_args, **_kwargs: io.StringIO(capabilities)),
        pytest.raises(RuntimeError, match="could not resolve the required io_uring_setup syscall"),
    ):
        exec(network_isolation._SECCOMP_INSTALL_CODE, {})


def test_seccomp_startup_paths_include_venv_site_packages(tmp_path):
    executable, base_site_packages, venv_site_packages = _create_seccomp_venv(tmp_path)

    startup_paths, import_paths, startup_tree_paths = network_isolation._seccomp_kernel_startup_paths(executable)

    assert startup_paths["pyvenv configuration"] == executable.parent.parent / "pyvenv.cfg"
    assert import_paths[0] == venv_site_packages
    assert base_site_packages not in import_paths
    assert venv_site_packages in startup_tree_paths


def test_seccomp_startup_paths_include_enabled_system_site_packages(tmp_path):
    executable, base_site_packages, venv_site_packages = _create_seccomp_venv(
        tmp_path,
        include_system_site_packages=True,
    )

    _, import_paths, _ = network_isolation._seccomp_kernel_startup_paths(executable)

    assert import_paths == (venv_site_packages, base_site_packages)


def test_seccomp_kernel_environment_rejects_kernel_owned_startup_paths():
    kernel_manager = MagicMock(spec=AsyncKernelManager)

    with (
        patch(
            "agora_workbench.code_execution.sessions.network_isolation._kernel_python_executable",
            return_value=network_isolation.Path("/opt/kernel-env/bin/python"),
        ),
        patch("agora_workbench.code_execution.sessions.network_isolation.os.geteuid", return_value=1000),
        patch(
            "agora_workbench.code_execution.sessions.network_isolation.Path.stat",
            return_value=SimpleNamespace(st_uid=1000),
        ),
        patch("agora_workbench.code_execution.sessions.network_isolation.os.access", return_value=False),
        patch("agora_workbench.code_execution.sessions.network_isolation.subprocess.run") as run,
        pytest.raises(RuntimeError, match="immutable Python environment"),
    ):
        network_isolation.validate_seccomp_kernel_environment(kernel_manager)

    run.assert_not_called()


def test_seccomp_kernel_environment_rejects_writable_parent_directory():
    kernel_manager = MagicMock(spec=AsyncKernelManager)
    startup_path = network_isolation.Path("/home/app/.cache/kernel-env/lib/python/site-packages")
    writable_parent = network_isolation.Path("/home/app/.cache")

    with (
        patch(
            "agora_workbench.code_execution.sessions.network_isolation._kernel_python_executable",
            return_value=startup_path / "bin" / "python",
        ),
        patch("agora_workbench.code_execution.sessions.network_isolation.os.geteuid", return_value=1000),
        patch(
            "agora_workbench.code_execution.sessions.network_isolation.Path.stat",
            return_value=SimpleNamespace(st_uid=0),
        ),
        patch(
            "agora_workbench.code_execution.sessions.network_isolation.os.access",
            side_effect=lambda path, mode: path == writable_parent,
        ),
        pytest.raises(RuntimeError, match=r"/home/app/\.cache"),
    ):
        network_isolation.validate_seccomp_kernel_environment(kernel_manager)


def test_seccomp_kernel_environment_rejects_kernel_owned_nested_module(tmp_path):
    executable, _, venv_site_packages = _create_seccomp_venv(tmp_path)
    mutable_module = venv_site_packages / "ipykernel" / "kernelapp.py"
    mutable_module.touch()
    kernel_manager = MagicMock(spec=AsyncKernelManager)

    with (
        patch(
            "agora_workbench.code_execution.sessions.network_isolation._kernel_python_executable",
            return_value=executable,
        ),
        patch("agora_workbench.code_execution.sessions.network_isolation.os.geteuid", return_value=1000),
        patch(
            "agora_workbench.code_execution.sessions.network_isolation._path_is_mutable_by_kernel",
            side_effect=lambda path, _stat_result, _kernel_uid: path == mutable_module,
        ),
        pytest.raises(RuntimeError, match="kernelapp.py"),
    ):
        network_isolation.validate_seccomp_kernel_environment(kernel_manager)


def test_seccomp_kernel_environment_rejects_mutable_symlink_target(tmp_path):
    executable, _, venv_site_packages = _create_seccomp_venv(tmp_path)
    mutable_target = tmp_path / "mutable-target.py"
    mutable_target.touch()
    (venv_site_packages / "ipykernel" / "linked.py").symlink_to(mutable_target)
    kernel_manager = MagicMock(spec=AsyncKernelManager)

    with (
        patch(
            "agora_workbench.code_execution.sessions.network_isolation._kernel_python_executable",
            return_value=executable,
        ),
        patch("agora_workbench.code_execution.sessions.network_isolation.os.geteuid", return_value=1000),
        patch(
            "agora_workbench.code_execution.sessions.network_isolation._path_is_mutable_by_kernel",
            side_effect=lambda path, _stat_result, _kernel_uid: path == mutable_target,
        ),
        pytest.raises(RuntimeError, match="mutable-target.py"),
    ):
        network_isolation.validate_seccomp_kernel_environment(kernel_manager)


def test_seccomp_kernel_bootstrap_skips_config_startup_and_pth_hooks(tmp_path):
    package_root = tmp_path / "site-packages"
    ipykernel_package = package_root / "ipykernel"
    ipykernel_package.mkdir(parents=True)
    (ipykernel_package / "__init__.py").touch()
    (ipykernel_package / "iostream.py").write_text(
        """
class IOPubThread:
    def __init__(self, socket, pipe=True, **kwargs):
        assert pipe is False
""",
        encoding="utf-8",
    )
    pth_marker = tmp_path / "pth-hook-ran"
    config_marker = tmp_path / "config-ran"
    path_marker = tmp_path / "path-ran"
    extensions_marker = tmp_path / "extensions-ran"
    startup_marker = tmp_path / "startup-ran"
    initialize_marker = tmp_path / "initialized"
    start_marker = tmp_path / "started"
    (package_root / "untrusted.pth").write_text(
        f"import pathlib; pathlib.Path({str(pth_marker)!r}).touch()\n",
        encoding="utf-8",
    )
    (ipykernel_package / "kernelapp.py").write_text(
        f"""
from pathlib import Path

class IPKernelApp:
    @classmethod
    def instance(cls):
        return cls()

    def __init__(self):
        self.exec_PYTHONSTARTUP = True
        self.exec_files = ["untrusted.py"]
        self.exec_lines = ["raise RuntimeError('untrusted')"]
        self.file_to_run = "untrusted.py"
        self.code_to_run = "raise RuntimeError('untrusted')"
        self.module_to_run = "untrusted"

    def load_config_file(self):
        Path({str(config_marker)!r}).touch()

    def init_path(self):
        Path({str(path_marker)!r}).touch()

    def init_extensions(self):
        Path({str(extensions_marker)!r}).touch()

    def _run_startup_files(self):
        Path({str(startup_marker)!r}).touch()

    def initialize(self):
        from ipykernel.iostream import IOPubThread
        IOPubThread(None, pipe=True)
        self.load_config_file()
        self.init_path()
        self.init_extensions()
        self._run_startup_files()
        assert self.exec_PYTHONSTARTUP is False
        assert self.exec_files == []
        assert self.exec_lines == []
        assert self.file_to_run == ""
        assert self.code_to_run == ""
        assert self.module_to_run == ""
        Path({str(initialize_marker)!r}).touch()

    def start(self):
        Path({str(start_marker)!r}).touch()
""",
        encoding="utf-8",
    )

    subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            network_isolation._seccomp_kernel_bootstrap_code((package_root,)),
            "-f",
            "connection.json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert initialize_marker.exists()
    assert start_marker.exists()
    assert not pth_marker.exists()
    assert not config_marker.exists()
    assert not path_marker.exists()
    assert not extensions_marker.exists()
    assert not startup_marker.exists()
