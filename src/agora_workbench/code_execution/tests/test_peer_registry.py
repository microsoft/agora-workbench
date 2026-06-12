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
