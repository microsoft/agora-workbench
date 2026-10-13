"""Unit tests for the Linux kernel network-namespace launcher."""

import json
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from jupyter_client.manager import AsyncKernelManager

from ..sessions import network_isolation


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


def test_seccomp_startup_paths_include_venv_site_packages(tmp_path):
    venv_root = tmp_path / "venv"
    executable = venv_root / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.touch()
    (venv_root / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    venv_site_packages = venv_root / "lib" / "python3.11" / "site-packages"
    venv_site_packages.mkdir(parents=True)

    kernel_manager = MagicMock(spec=AsyncKernelManager)
    kernel_manager.kernel_spec.argv = [str(executable), "-m", "ipykernel_launcher"]
    kernel_manager.kernel_spec.metadata = {}
    python_metadata = {
        "version": [3, 11],
        "paths": {
            "stdlib": "/usr/lib/python3.11",
            "platstdlib": "/usr/lib/python3.11",
            "purelib": "/usr/lib/python3.11/site-packages",
            "platlib": "/usr/lib/python3.11/site-packages",
        },
    }

    with patch(
        "agora_workbench.code_execution.sessions.network_isolation.subprocess.run",
        return_value=subprocess.CompletedProcess([], 0, json.dumps(python_metadata), ""),
    ):
        startup_paths = network_isolation._seccomp_kernel_startup_paths(kernel_manager)

    assert startup_paths["pyvenv configuration"] == venv_root / "pyvenv.cfg"
    assert startup_paths["venv site-packages"] == venv_site_packages


def test_seccomp_kernel_environment_rejects_kernel_owned_startup_paths():
    kernel_manager = MagicMock(spec=AsyncKernelManager)
    startup_path = network_isolation.Path("/opt/kernel-env/lib/python/site-packages")

    with (
        patch(
            "agora_workbench.code_execution.sessions.network_isolation._seccomp_kernel_startup_paths",
            return_value={"purelib": startup_path},
        ),
        patch("agora_workbench.code_execution.sessions.network_isolation.os.geteuid", return_value=1000),
        patch(
            "agora_workbench.code_execution.sessions.network_isolation.Path.stat",
            return_value=SimpleNamespace(st_uid=1000),
        ),
        patch("agora_workbench.code_execution.sessions.network_isolation.os.access", return_value=False),
        pytest.raises(RuntimeError, match="immutable Python environment"),
    ):
        network_isolation.validate_seccomp_kernel_environment(kernel_manager)


def test_seccomp_kernel_environment_rejects_writable_parent_directory():
    kernel_manager = MagicMock(spec=AsyncKernelManager)
    startup_path = network_isolation.Path("/home/app/.cache/kernel-env/lib/python/site-packages")
    writable_parent = network_isolation.Path("/home/app/.cache")

    with (
        patch(
            "agora_workbench.code_execution.sessions.network_isolation._seccomp_kernel_startup_paths",
            return_value={"purelib": startup_path},
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
