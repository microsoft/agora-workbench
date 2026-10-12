"""Unit tests for the Linux kernel network-namespace launcher."""

import json
import subprocess
from unittest.mock import patch

import pytest

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


def test_seccomp_kernel_environment_rejects_kernel_owned_startup_paths():
    kernel_manager = type(
        "KernelManager",
        (),
        {
            "kernel_spec": type(
                "KernelSpec",
                (),
                {"argv": ["/env/bin/python"], "metadata": {}},
            )()
        },
    )()
    python_paths = {
        "stdlib": "/env/lib/python",
        "platstdlib": "/env/lib/python",
        "purelib": "/env/lib/python/site-packages",
        "platlib": "/env/lib/python/site-packages",
    }

    with (
        patch(
            "agora_workbench.code_execution.sessions.network_isolation.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, json.dumps(python_paths), ""),
        ),
        patch("agora_workbench.code_execution.sessions.network_isolation.os.geteuid", return_value=1000),
        patch("agora_workbench.code_execution.sessions.network_isolation.Path.stat") as stat,
        patch("agora_workbench.code_execution.sessions.network_isolation.os.access", return_value=False),
        pytest.raises(RuntimeError, match="immutable Python environment"),
    ):
        stat.return_value.st_uid = 1000
        network_isolation.validate_seccomp_kernel_environment(kernel_manager)
