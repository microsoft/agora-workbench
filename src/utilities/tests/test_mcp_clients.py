"""Tests for utilities.mcp_clients.connect_mcp_servers."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from utilities.mcp_clients import (
    _build_http_client,
    _is_local,
    _load_registry,
    _select,
    connect_mcp_servers,
)


# ---------------------------------------------------------------------------
# _load_registry
# ---------------------------------------------------------------------------


def _write_registry(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "server_registry.yaml"
    p.write_text(content, encoding="utf-8")
    return p


class TestLoadRegistry:
    def test_well_formed(self, tmp_path):
        path = _write_registry(
            tmp_path,
            """
scope: api://foo/.default
servers:
  - name: a
    url: http://localhost:8001/mcp
  - name: b
    url: http://localhost:8002/mcp
""",
        )
        data = _load_registry(path)
        assert data["scope"] == "api://foo/.default"
        assert len(data["servers"]) == 2

    def test_missing_optional_fields(self, tmp_path):
        path = _write_registry(tmp_path, "servers: []\n")
        data = _load_registry(path)
        assert data["servers"] == []
        assert "scope" not in data

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            _load_registry(tmp_path / "nope.yaml")

    def test_non_mapping_root_raises(self, tmp_path):
        path = _write_registry(tmp_path, "- a\n- b\n")
        with pytest.raises(ValueError, match="root must be a mapping"):
            _load_registry(path)

    def test_servers_not_a_list_raises(self, tmp_path):
        path = _write_registry(tmp_path, "servers: not-a-list\n")
        with pytest.raises(ValueError, match="must be a list"):
            _load_registry(path)


# ---------------------------------------------------------------------------
# _select (filtering by name)
# ---------------------------------------------------------------------------


_AVAILABLE = [
    {"name": "alpha", "url": "http://localhost:1/mcp"},
    {"name": "beta", "url": "http://localhost:2/mcp"},
    {"name": "gamma", "url": "http://localhost:3/mcp"},
]


class TestSelect:
    def test_none_returns_all(self, monkeypatch):
        monkeypatch.delenv("WORKBENCH_SERVERS", raising=False)
        assert _select(None, _AVAILABLE) == _AVAILABLE

    def test_filter_by_argument(self, monkeypatch):
        monkeypatch.delenv("WORKBENCH_SERVERS", raising=False)
        selected = _select(["alpha", "gamma"], _AVAILABLE)
        assert [s["name"] for s in selected] == ["alpha", "gamma"]

    def test_env_var_overrides_argument(self, monkeypatch):
        monkeypatch.setenv("WORKBENCH_SERVERS", "beta")
        selected = _select(["alpha", "gamma"], _AVAILABLE)
        assert [s["name"] for s in selected] == ["beta"]

    def test_env_var_strips_and_splits(self, monkeypatch):
        monkeypatch.setenv("WORKBENCH_SERVERS", " alpha , beta ")
        selected = _select(None, _AVAILABLE)
        assert [s["name"] for s in selected] == ["alpha", "beta"]

    def test_unknown_name_warns_and_skips(self, monkeypatch, caplog):
        monkeypatch.delenv("WORKBENCH_SERVERS", raising=False)
        with caplog.at_level(logging.WARNING):
            selected = _select(["alpha", "missing"], _AVAILABLE)
        assert [s["name"] for s in selected] == ["alpha"]
        assert "missing" in caplog.text


# ---------------------------------------------------------------------------
# _is_local + _build_http_client
# ---------------------------------------------------------------------------


class TestIsLocal:
    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost:8000/mcp",
            "http://127.0.0.1:8000/mcp",
            "http://[::1]:8000/mcp",
        ],
    )
    def test_local_urls(self, url):
        assert _is_local(url)

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/mcp",
            "https://my-server.azurewebsites.net/mcp",
        ],
    )
    def test_non_local_urls(self, url):
        assert not _is_local(url)


class TestBuildHttpClient:
    @pytest.mark.asyncio
    async def test_localhost_uses_dev_bearer(self):
        client = _build_http_client("http://localhost:8021/mcp", scope=None)
        try:
            assert client.headers["Authorization"] == "Bearer dev-token"
        finally:
            await client.aclose()

    def test_non_local_requires_scope(self):
        with pytest.raises(ValueError, match="requires a 'scope'"):
            _build_http_client("https://example.com/mcp", scope=None)

    @pytest.mark.asyncio
    async def test_non_local_uses_token_provider(self):
        with patch("utilities.auth.get_token_provider") as mock_get:
            mock_provider = MagicMock(return_value="real-token")
            mock_get.return_value = mock_provider
            client = _build_http_client("https://example.com/mcp", scope="api://x/.default")
            try:
                mock_get.assert_called_once_with("api://x/.default")
                # Auth class is set; we don't exercise the flow here.
                assert client.auth is not None
            finally:
                await client.aclose()


# ---------------------------------------------------------------------------
# connect_mcp_servers (integration with mocked httpx)
# ---------------------------------------------------------------------------


@pytest.fixture
def registry_yaml(tmp_path):
    return _write_registry(
        tmp_path,
        """
scope: api://workbench/.default
servers:
  - name: alpha
    url: http://localhost:8001/mcp
  - name: beta
    url: http://localhost:8002/mcp
""",
    )


def _make_async_client_factory(health_response_per_url):
    """Build a fake AsyncClient class that returns predetermined health responses."""

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            self.headers = kwargs.get("headers", {})
            self.auth = kwargs.get("auth")
            self.closed = False

        async def get(self, url, timeout=None):
            outcome = health_response_per_url.get(url, "raise")
            if outcome == "raise":
                raise httpx.ConnectError(f"refused: {url}")
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            if outcome == "5xx":
                resp.raise_for_status.side_effect = httpx.HTTPStatusError(
                    "boom", request=MagicMock(), response=MagicMock()
                )
            return resp

        async def aclose(self):
            self.closed = True

    return FakeAsyncClient


class TestConnectMcpServers:
    @pytest.mark.asyncio
    async def test_all_healthy_returns_all(self, registry_yaml, monkeypatch):
        monkeypatch.delenv("WORKBENCH_SERVERS", raising=False)
        Fake = _make_async_client_factory(
            {
                "http://localhost:8001/health": "ok",
                "http://localhost:8002/health": "ok",
            }
        )
        with patch("utilities.mcp_clients.httpx.AsyncClient", Fake):
            servers = await connect_mcp_servers(registry_path=registry_yaml)
        assert [s.name for s in servers] == ["alpha", "beta"]
        for s in servers:
            await s.http_client.aclose()

    @pytest.mark.asyncio
    async def test_unhealthy_is_skipped(self, registry_yaml, monkeypatch, caplog):
        monkeypatch.delenv("WORKBENCH_SERVERS", raising=False)
        Fake = _make_async_client_factory(
            {
                "http://localhost:8001/health": "ok",
                "http://localhost:8002/health": "raise",
            }
        )
        with patch("utilities.mcp_clients.httpx.AsyncClient", Fake):
            with caplog.at_level(logging.WARNING):
                servers = await connect_mcp_servers(registry_path=registry_yaml)
        assert [s.name for s in servers] == ["alpha"]
        assert "unreachable" in caplog.text
        for s in servers:
            await s.http_client.aclose()

    @pytest.mark.asyncio
    async def test_5xx_is_skipped(self, registry_yaml, monkeypatch):
        monkeypatch.delenv("WORKBENCH_SERVERS", raising=False)
        Fake = _make_async_client_factory(
            {
                "http://localhost:8001/health": "ok",
                "http://localhost:8002/health": "5xx",
            }
        )
        with patch("utilities.mcp_clients.httpx.AsyncClient", Fake):
            servers = await connect_mcp_servers(registry_path=registry_yaml)
        assert [s.name for s in servers] == ["alpha"]
        for s in servers:
            await s.http_client.aclose()

    @pytest.mark.asyncio
    async def test_all_unreachable_returns_empty(self, registry_yaml, monkeypatch):
        monkeypatch.delenv("WORKBENCH_SERVERS", raising=False)
        Fake = _make_async_client_factory(
            {
                "http://localhost:8001/health": "raise",
                "http://localhost:8002/health": "raise",
            }
        )
        with patch("utilities.mcp_clients.httpx.AsyncClient", Fake):
            servers = await connect_mcp_servers(registry_path=registry_yaml)
        assert servers == []

    @pytest.mark.asyncio
    async def test_servers_filter_applied(self, registry_yaml, monkeypatch):
        monkeypatch.delenv("WORKBENCH_SERVERS", raising=False)
        Fake = _make_async_client_factory(
            {
                "http://localhost:8001/health": "ok",
                "http://localhost:8002/health": "ok",
            }
        )
        with patch("utilities.mcp_clients.httpx.AsyncClient", Fake):
            servers = await connect_mcp_servers(
                servers=["beta"], registry_path=registry_yaml
            )
        assert [s.name for s in servers] == ["beta"]
        for s in servers:
            await s.http_client.aclose()

    @pytest.mark.asyncio
    async def test_env_var_filter_applied(self, registry_yaml, monkeypatch):
        monkeypatch.setenv("WORKBENCH_SERVERS", "alpha")
        Fake = _make_async_client_factory(
            {
                "http://localhost:8001/health": "ok",
                "http://localhost:8002/health": "ok",
            }
        )
        with patch("utilities.mcp_clients.httpx.AsyncClient", Fake):
            servers = await connect_mcp_servers(
                servers=["beta"], registry_path=registry_yaml
            )
        # Env var wins over the servers= argument.
        assert [s.name for s in servers] == ["alpha"]
        for s in servers:
            await s.http_client.aclose()

    @pytest.mark.asyncio
    async def test_entry_missing_url_skipped(self, tmp_path, monkeypatch, caplog):
        monkeypatch.delenv("WORKBENCH_SERVERS", raising=False)
        path = _write_registry(
            tmp_path,
            """
servers:
  - name: alpha
  - name: beta
    url: http://localhost:8002/mcp
""",
        )
        Fake = _make_async_client_factory(
            {"http://localhost:8002/health": "ok"}
        )
        with patch("utilities.mcp_clients.httpx.AsyncClient", Fake):
            with caplog.at_level(logging.WARNING):
                servers = await connect_mcp_servers(registry_path=path)
        assert [s.name for s in servers] == ["beta"]
        assert "missing 'name' or 'url'" in caplog.text
        for s in servers:
            await s.http_client.aclose()
