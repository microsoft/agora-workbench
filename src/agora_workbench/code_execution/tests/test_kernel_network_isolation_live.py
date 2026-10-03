"""Live validation for Linux kernel network namespace isolation."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from ..sessions import SessionConfig, SessionManager
from ..sessions.network_isolation import validate_network_isolation_support


pytestmark = [pytest.mark.live, pytest.mark.asyncio]


async def _execute_and_collect(kernel_client, code: str) -> str:
    msg_id = kernel_client.execute(code)
    stdout: list[str] = []
    while True:
        message = await kernel_client.get_iopub_msg(timeout=10)
        if message.get("parent_header", {}).get("msg_id") != msg_id:
            continue
        message_type = message.get("msg_type")
        if message_type == "stream":
            stdout.append(message.get("content", {}).get("text", ""))
        elif message_type == "error":
            pytest.fail("\n".join(message.get("content", {}).get("traceback", [])))
        elif message_type == "status" and message.get("content", {}).get("execution_state") == "idle":
            return "".join(stdout)


async def test_isolated_kernel_and_subprocess_have_no_ip_network():
    try:
        validate_network_isolation_support()
    except RuntimeError as exc:
        pytest.skip(str(exc))

    manager = SessionManager(SessionConfig(kernel_network_mode="isolated"))
    session_id = manager.create_session({}, "live-test", "", {})
    host_listener = socket.socket()
    host_listener.bind(("127.0.0.1", 0))
    host_listener.listen()
    host_port = host_listener.getsockname()[1]
    ipc_dir: Path | None = None
    try:
        kernel_manager, kernel_client = await manager._get_or_create_kernel(session_id)
        ipc_dir = Path(kernel_manager.ip).parent
        output = await _execute_and_collect(
            kernel_client,
            f"""
import json
import socket
import subprocess
import sys

status = dict(
    line.rstrip().split(":", 1)
    for line in open("/proc/self/status", encoding="utf-8")
    if ":" in line
)

def connect_error(host, port):
    sock = socket.socket()
    sock.settimeout(1)
    try:
        sock.connect((host, port))
    except OSError as exc:
        return {{"type": type(exc).__name__, "errno": exc.errno}}
    finally:
        sock.close()
    return {{"type": None, "errno": None}}

def private_loopback_available():
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return True
    except OSError:
        return False
    finally:
        sock.close()

child = subprocess.run(
    [
        sys.executable,
        "-c",
        "import json,socket; s=socket.socket(); s.settimeout(1); "
        "\\ntry: s.connect(('198.51.100.1',80)); r={{'type':None,'errno':None}}"
        "\\nexcept OSError as e: r={{'type':type(e).__name__,'errno':e.errno}}"
        "\\nfinally: s.close()"
        "\\nprint(json.dumps(r))",
    ],
    check=True,
    capture_output=True,
    text=True,
)
print(json.dumps({{
    "private_loopback": private_loopback_available(),
    "host_loopback": connect_error("127.0.0.1", {host_port}),
    "external": connect_error("198.51.100.1", 80),
    "child_external": json.loads(child.stdout),
    "cap_eff": status["CapEff"].strip(),
    "cap_bnd": status["CapBnd"].strip(),
    "no_new_privs": status["NoNewPrivs"].strip(),
}}))
""",
        )
        result = json.loads(output)

        assert result["private_loopback"] is True
        assert result["host_loopback"] == {"type": "ConnectionRefusedError", "errno": 111}
        assert result["external"]["type"] == "OSError"
        assert result["external"]["errno"] in {50, 51, 65, 100, 101, 113}
        assert result["child_external"]["type"] == "OSError"
        assert result["child_external"]["errno"] in {50, 51, 65, 100, 101, 113}
        assert int(result["cap_eff"], 16) == 0
        assert int(result["cap_bnd"], 16) == 0
        assert result["no_new_privs"] == "1"
    finally:
        host_listener.close()
        await manager.aclose_session(session_id)
        if ipc_dir is not None:
            assert not ipc_dir.exists()
