"""Tests for the shared peer registry that backs dynamic ``{name}_send`` destinations.

The registry lets the unified send tool resolve a peer server by name at call
time (constructing a ``ServerPublisher`` on demand) instead of requiring one
pre-registered publisher per peer — O(N) operator config instead of O(N²).
"""

from __future__ import annotations

import json

import pytest

from .. import CodeExecutionServer, ServerConfig
from ..auth import create_noop_auth_config


def _make_server(peer_registry: dict[str, str] | None = None, name: str = "alpha") -> CodeExecutionServer:
    config = ServerConfig(
        name=name,
        description=f"Per-tool execute_{name}_code description.",
        type="uv",
        dependency_file="numpy\n",
        auto_build=False,
        peer_registry=peer_registry or {},
    )
    return CodeExecutionServer(server_config=config, auth_config=create_noop_auth_config())


class TestLoadPeerRegistry:
    @pytest.mark.unit
    def test_from_config(self):
        server = _make_server({"beta": "https://beta:8000", "gamma": "https://gamma:8000"})
        assert server._peer_registry == {"beta": "https://beta:8000", "gamma": "https://gamma:8000"}

    @pytest.mark.unit
    def test_drops_self(self):
        """A server never sends to itself, even if its own name is in the map."""
        server = _make_server({"alpha": "https://alpha:8000", "beta": "https://beta:8000"}, name="alpha")
        assert "alpha" not in server._peer_registry
        assert server._peer_registry == {"beta": "https://beta:8000"}

    @pytest.mark.unit
    def test_env_inline_json_overrides_config(self, monkeypatch):
        monkeypatch.setenv(
            "AGORA_PEER_REGISTRY",
            json.dumps({"beta": "https://override:8000", "delta": "https://delta:8000"}),
        )
        server = _make_server({"beta": "https://beta:8000"})
        assert server._peer_registry["beta"] == "https://override:8000"  # env wins
        assert server._peer_registry["delta"] == "https://delta:8000"  # env adds

    @pytest.mark.unit
    def test_env_json_file(self, monkeypatch, tmp_path):
        reg_file = tmp_path / "registry.json"
        reg_file.write_text(json.dumps({"beta": "https://beta:8000"}))
        monkeypatch.setenv("AGORA_PEER_REGISTRY", str(reg_file))
        server = _make_server()
        assert server._peer_registry == {"beta": "https://beta:8000"}

    @pytest.mark.unit
    def test_bad_env_is_ignored(self, monkeypatch):
        """Malformed env value must not break server startup — config still applies."""
        monkeypatch.setenv("AGORA_PEER_REGISTRY", "this is not json")
        server = _make_server({"beta": "https://beta:8000"})
        assert server._peer_registry == {"beta": "https://beta:8000"}

    @pytest.mark.unit
    def test_non_dict_env_is_ignored(self, monkeypatch):
        monkeypatch.setenv("AGORA_PEER_REGISTRY", json.dumps(["beta", "gamma"]))
        server = _make_server({"beta": "https://beta:8000"})
        assert server._peer_registry == {"beta": "https://beta:8000"}


class TestSendToolSurfacesRegistry:
    @pytest.mark.asyncio
    async def test_registry_peer_listed_as_destination(self):
        """A registry peer must appear in the send tool's destinations so the agent
        knows it can send there — even with no ServerPublisher pre-registered."""
        server = _make_server({"earthscience": "https://earthscience:8000"})
        send_tool = await server.mcp.get_tool("alpha_send")
        description = send_tool.description or ""
        assert "earthscience" in description
        # The default 'user' destination (GuiPublisher) is always present too.
        assert "user" in description


class TestRegistryHttpPeerIsTrusted:
    """A plain-HTTP peer configured in the registry should not also require the
    operator to list it in OBJECT_TRANSFER_TRUSTED_HTTP_HOSTS. The send tool
    builds the on-demand ServerPublisher with ``trust_http=True`` because the
    operator already chose the scheme in the registry URL."""

    @pytest.mark.unit
    def test_registry_built_publisher_trusts_http(self):
        """The publisher constructed for a registry peer carries trust_http=True."""
        from ..data_access.publishers import ServerPublisher

        # Mirror how the send tool constructs the on-demand publisher.
        pub = ServerPublisher(server_name="earthscience", target_url="http://earthscience-server:8000", trust_http=True)
        assert pub._trust_http is True

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_registry_http_peer_passes_validation_without_env(self, monkeypatch, tmp_path):
        """A registry http:// peer publishes successfully with no trusted-host env var."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from ..data_access.publishers import ServerPublisher

        monkeypatch.delenv("OBJECT_TRANSFER_TRUSTED_HTTP_HOSTS", raising=False)

        publisher = ServerPublisher(
            server_name="earthscience",
            target_url="http://earthscience-server:8000",
            trust_http=True,
        )
        publisher._user_token = "tok"
        publisher._source_server = "alpha"
        publisher._transfer_id = ""

        pkl_file = tmp_path / "data.pkl"
        pkl_file.write_bytes(b"data")

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"success": True}

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            # Would raise ValueError("Plain HTTP ...") without trust_http.
            await publisher.publish(local_path=pkl_file, name="my_var", session_id="")
            url = mock_client.post.call_args[0][0]
            assert url == "http://earthscience-server:8000/object-transfer/receive"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_static_publisher_still_requires_env(self, monkeypatch, tmp_path):
        """A statically-built publisher (not from the registry) still needs the env var."""
        from ..data_access.publishers import ServerPublisher

        monkeypatch.delenv("OBJECT_TRANSFER_TRUSTED_HTTP_HOSTS", raising=False)

        publisher = ServerPublisher(server_name="gis", target_url="http://gis-server:8000")
        publisher._user_token = "tok"
        publisher._source_server = "alpha"
        publisher._transfer_id = ""

        pkl_file = tmp_path / "data.pkl"
        pkl_file.write_bytes(b"data")

        with pytest.raises(ValueError, match="Plain HTTP"):
            await publisher.publish(local_path=pkl_file, name="my_var", session_id="")

