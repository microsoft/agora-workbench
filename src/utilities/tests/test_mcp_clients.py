"""Tests for utilities.mcp_clients."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from utilities.mcp_clients import (
    McpServerConfig,
    _build_http_client,
    _is_local,
    connect_mcp_servers,
    load_server_registry,
)


# ---------------------------------------------------------------------------
# McpServerConfig
# ---------------------------------------------------------------------------


class TestMcpServerConfig:
    def test_minimum_fields(self):
        cfg = McpServerConfig(name="x", url="http://localhost:1/mcp")
        assert cfg.name == "x"
        assert cfg.url == "http://localhost:1/mcp"
        assert cfg.scope is None

    def test_with_scope(self):
        cfg = McpServerConfig(name="x", url="https://x.com/mcp", scope="api://x/.default")
        assert cfg.scope == "api://x/.default"


# ---------------------------------------------------------------------------
# load_server_registry
# ---------------------------------------------------------------------------


def _write_yaml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "servers.yaml"
    p.write_text(content, encoding="utf-8")
    return p


class TestLoadServerRegistry:
    def test_well_formed(self, tmp_path):
        path = _write_yaml(
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
        configs = load_server_registry(path)
        assert [c.name for c in configs] == ["a", "b"]
        # Both inherit the top-level default scope.
        assert configs[0].scope == "api://foo/.default"

    def test_per_entry_scope_overrides_default(self, tmp_path):
        path = _write_yaml(
            tmp_path,
            """
scope: api://default/.default
servers:
  - name: a
    url: https://a.example.com/mcp
    scope: api://custom/.default
""",
        )
        configs = load_server_registry(path)
        assert configs[0].scope == "api://custom/.default"

    def test_no_default_scope(self, tmp_path):
        path = _write_yaml(
            tmp_path,
            """
servers:
  - name: a
    url: http://localhost:8001/mcp
""",
        )
        configs = load_server_registry(path)
        assert configs[0].scope is None

    def test_empty_servers_list(self, tmp_path):
        path = _write_yaml(tmp_path, "servers: []\n")
        assert load_server_registry(path) == []

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_server_registry(tmp_path / "nope.yaml")

    def test_non_mapping_root_raises(self, tmp_path):
        path = _write_yaml(tmp_path, "- a\n- b\n")
        with pytest.raises(ValueError, match="root must be a mapping"):
            load_server_registry(path)

    def test_servers_not_a_list_raises(self, tmp_path):
        path = _write_yaml(tmp_path, "servers: not-a-list\n")
        with pytest.raises(ValueError, match="must be a list"):
            load_server_registry(path)

    def test_entry_missing_name_or_url_raises(self, tmp_path):
        path = _write_yaml(
            tmp_path,
            """
servers:
  - name: a
""",
        )
        with pytest.raises(ValueError, match="missing 'name' or 'url'"):
            load_server_registry(path)


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
                assert client.auth is not None
            finally:
                await client.aclose()


# ---------------------------------------------------------------------------
# connect_mcp_servers
# ---------------------------------------------------------------------------


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


_TWO_LOCAL = [
    McpServerConfig(name="alpha", url="http://localhost:8001/mcp"),
    McpServerConfig(name="beta", url="http://localhost:8002/mcp"),
]


class TestConnectMcpServers:
    @pytest.mark.asyncio
    async def test_empty_list_returns_empty(self):
        assert await connect_mcp_servers([]) == []

    @pytest.mark.asyncio
    async def test_all_healthy_returns_all(self):
        Fake = _make_async_client_factory(
            {
                "http://localhost:8001/health": "ok",
                "http://localhost:8002/health": "ok",
            }
        )
        with patch("utilities.mcp_clients.httpx.AsyncClient", Fake):
            servers = await connect_mcp_servers(_TWO_LOCAL)
        assert [s.name for s in servers] == ["alpha", "beta"]
        for s in servers:
            await s.http_client.aclose()

    @pytest.mark.asyncio
    async def test_unhealthy_is_skipped(self, caplog):
        Fake = _make_async_client_factory(
            {
                "http://localhost:8001/health": "ok",
                "http://localhost:8002/health": "raise",
            }
        )
        with patch("utilities.mcp_clients.httpx.AsyncClient", Fake):
            with caplog.at_level(logging.WARNING):
                servers = await connect_mcp_servers(_TWO_LOCAL)
        assert [s.name for s in servers] == ["alpha"]
        assert "unreachable" in caplog.text
        for s in servers:
            await s.http_client.aclose()

    @pytest.mark.asyncio
    async def test_5xx_is_skipped(self):
        Fake = _make_async_client_factory(
            {
                "http://localhost:8001/health": "ok",
                "http://localhost:8002/health": "5xx",
            }
        )
        with patch("utilities.mcp_clients.httpx.AsyncClient", Fake):
            servers = await connect_mcp_servers(_TWO_LOCAL)
        assert [s.name for s in servers] == ["alpha"]
        for s in servers:
            await s.http_client.aclose()

    @pytest.mark.asyncio
    async def test_all_unreachable_returns_empty(self):
        Fake = _make_async_client_factory(
            {
                "http://localhost:8001/health": "raise",
                "http://localhost:8002/health": "raise",
            }
        )
        with patch("utilities.mcp_clients.httpx.AsyncClient", Fake):
            servers = await connect_mcp_servers(_TWO_LOCAL)
        assert servers == []

    @pytest.mark.asyncio
    async def test_load_then_connect_flow(self, tmp_path):
        """End-to-end shape: yaml loader feeds connect_mcp_servers."""
        path = _write_yaml(
            tmp_path,
            """
servers:
  - name: alpha
    url: http://localhost:8001/mcp
""",
        )
        Fake = _make_async_client_factory({"http://localhost:8001/health": "ok"})
        with patch("utilities.mcp_clients.httpx.AsyncClient", Fake):
            configs = load_server_registry(path)
            servers = await connect_mcp_servers(configs)
        assert [s.name for s in servers] == ["alpha"]
        for s in servers:
            await s.http_client.aclose()
