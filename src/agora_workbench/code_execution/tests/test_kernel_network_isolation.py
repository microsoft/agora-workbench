"""Unit tests for the Linux kernel network-namespace launcher."""

from unittest.mock import patch

import pytest

from ..sessions import network_isolation


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
