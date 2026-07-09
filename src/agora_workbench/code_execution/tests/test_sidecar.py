"""Tests for SidecarConfig validation and SidecarManager lifecycle."""

import os
import sys
import textwrap

import pytest
from pydantic import ValidationError

from ..code_execution_models import ServerConfig, SidecarConfig
from ..sidecar import SidecarManager


def _server_config(**kwargs) -> ServerConfig:
    """Build a minimal valid ServerConfig with the required fields filled in."""
    return ServerConfig(
        name=kwargs.pop("name", "demo"),
        description=kwargs.pop("description", "demo server"),
        type=kwargs.pop("type", "conda"),
        dependency_file=kwargs.pop("dependency_file", "numpy\n"),
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# SidecarConfig validation
# --------------------------------------------------------------------------- #
def test_sidecar_config_defaults():
    spec = SidecarConfig(
        name="model",
        command=["-m", "mypkg.service"],
        url_env_var="MYPKG_SERVICE_URL",
        port=9100,
    )
    assert spec.use_env_python is True
    assert spec.host == "127.0.0.1"
    assert spec.health_path == "/health"
    assert spec.readiness_timeout_s == 120.0
    assert spec.env == {}
    assert spec.base_url() == "http://127.0.0.1:9100"
    assert spec.health_url() == "http://127.0.0.1:9100/health"


def test_sidecar_config_requires_port():
    with pytest.raises(ValidationError):
        SidecarConfig(name="m", command=["-m", "x"], url_env_var="U")


@pytest.mark.parametrize("bad_port", [0, -1, 65536, 99999])
def test_sidecar_config_rejects_out_of_range_port(bad_port):
    with pytest.raises(ValidationError):
        SidecarConfig(name="m", command=["-m", "x"], url_env_var="U", port=bad_port)


def test_sidecar_config_health_url_normalizes_missing_slash():
    spec = SidecarConfig(
        name="m",
        command=["-m", "x"],
        url_env_var="U",
        port=9100,
        health_path="ready",
    )
    assert spec.health_url() == "http://127.0.0.1:9100/ready"


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.5", "::1", "localhost"])
def test_sidecar_config_allows_loopback_hosts(host):
    spec = SidecarConfig(name="m", command=["-m", "x"], url_env_var="U", port=9100, host=host)
    assert spec.host == host


@pytest.mark.parametrize("host", ["0.0.0.0", "10.0.0.5", "example.com", ""])
def test_sidecar_config_rejects_non_loopback_hosts(host):
    with pytest.raises(ValidationError):
        SidecarConfig(name="m", command=["-m", "x"], url_env_var="U", port=9100, host=host)


def test_server_config_sidecars_default_empty():
    config = _server_config()
    assert config.sidecars == []


def test_server_config_accepts_sidecars():
    config = _server_config(
        sidecars=[
            SidecarConfig(name="model", command=["-m", "svc"], url_env_var="SVC_URL", port=9100)
        ],
    )
    assert len(config.sidecars) == 1
    assert config.sidecars[0].name == "model"


# --------------------------------------------------------------------------- #
# SidecarManager lifecycle
# --------------------------------------------------------------------------- #
def _fake_sidecar_script(tmp_path):
    """A trivial stdlib HTTP server that binds SIDECAR_HOST/PORT and serves /health."""
    script = tmp_path / "fake_sidecar.py"
    script.write_text(
        textwrap.dedent(
            """
            import os
            from http.server import BaseHTTPRequestHandler, HTTPServer

            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    if self.path == "/health":
                        self.send_response(200)
                        self.end_headers()
                        self.wfile.write(b"ok")
                    else:
                        self.send_response(404)
                        self.end_headers()

                def log_message(self, *args):
                    pass

            host = os.environ["SIDECAR_HOST"]
            port = int(os.environ["SIDECAR_PORT"])
            HTTPServer((host, port), Handler).serve_forever()
            """
        )
    )
    return script


def _free_port():
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.asyncio
async def test_manager_no_sidecars_is_noop():
    config = _server_config()
    manager = SidecarManager(config)
    await manager.start_all()
    assert manager.running is False
    await manager.stop_all()


@pytest.mark.asyncio
async def test_manager_starts_health_checks_and_injects_env(tmp_path, monkeypatch):
    script = _fake_sidecar_script(tmp_path)
    port = _free_port()
    env_var = "FAKE_SIDECAR_URL"
    monkeypatch.delenv(env_var, raising=False)

    config = _server_config(
        sidecars=[
            SidecarConfig(
                name="fake",
                command=[sys.executable, str(script)],
                use_env_python=False,  # run with the current interpreter directly
                url_env_var=env_var,
                port=port,
                readiness_timeout_s=30.0,
            )
        ],
    )

    manager = SidecarManager(config)
    try:
        await manager.start_all()
        assert manager.running is True
        assert os.environ[env_var] == f"http://127.0.0.1:{port}"

        import httpx

        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"http://127.0.0.1:{port}/health")
            assert resp.status_code == 200
    finally:
        await manager.stop_all()
        os.environ.pop(env_var, None)
    assert manager.running is False
    # Shutdown must unset the now-stale discovery URL so it does not leak into
    # later tests (or mislead kernels about a sidecar that is gone).
    assert env_var not in os.environ


def test_build_env_reserves_sidecar_host_and_port():
    """Caller-supplied SIDECAR_HOST/PORT must not override the configured bind."""
    spec = SidecarConfig(
        name="fake",
        command=["-m", "svc"],
        url_env_var="SVC_URL",
        port=9100,
        host="127.0.0.1",
        env={"SIDECAR_HOST": "0.0.0.0", "SIDECAR_PORT": "1", "EXTRA": "keep"},
    )
    manager = SidecarManager(_server_config(sidecars=[spec]))
    env = manager._build_env(spec)
    assert env["SIDECAR_HOST"] == "127.0.0.1"
    assert env["SIDECAR_PORT"] == "9100"
    assert env["EXTRA"] == "keep"


@pytest.mark.asyncio
async def test_manager_times_out_when_health_never_ready(tmp_path):
    # A process that exits immediately -> never serves health.
    port = _free_port()
    config = _server_config(
        sidecars=[
            SidecarConfig(
                name="dead",
                command=[sys.executable, "-c", "import sys; sys.exit(0)"],
                use_env_python=False,
                url_env_var="DEAD_URL",
                port=port,
                readiness_timeout_s=5.0,
            )
        ],
    )
    manager = SidecarManager(config)
    with pytest.raises(RuntimeError, match="exited during startup"):
        await manager.start_all()
    # Failed startup must leave nothing running.
    assert manager.running is False
