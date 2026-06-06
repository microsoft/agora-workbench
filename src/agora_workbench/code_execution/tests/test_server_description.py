"""Tests for the optional ``ServerConfig.server_description`` field
and its propagation into FastMCP's ``instructions`` slot.
"""

from __future__ import annotations

from .. import CodeExecutionServer, ServerConfig
from ..auth import create_noop_auth_config


def _make_server(server_description: str | None) -> CodeExecutionServer:
    config = ServerConfig(
        name="test_desc",
        description="Per-tool execute_test_desc_code description text.",
        server_description=server_description,
        type="uv",
        dependency_file="numpy\n",
        auto_build=False,
    )
    return CodeExecutionServer(
        server_config=config,
        auth_config=create_noop_auth_config(),
    )


class TestServerDescriptionField:
    def test_falls_back_to_description_when_unset(self):
        server = _make_server(server_description=None)
        assert server.mcp.instructions == "Per-tool execute_test_desc_code description text."

    def test_explicit_server_description_wins(self):
        server = _make_server(
            server_description="This server is the canonical earth-science kernel.",
        )
        assert server.mcp.instructions == "This server is the canonical earth-science kernel."

    def test_field_is_optional(self):
        """Constructing ServerConfig without server_description should not raise."""
        config = ServerConfig(
            name="x",
            description="d",
            type="uv",
            dependency_file="numpy\n",
        )
        assert config.server_description is None
