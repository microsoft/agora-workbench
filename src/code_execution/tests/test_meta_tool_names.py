"""Regression test for MCP meta-tool name prefixing.

Every meta-tool the server auto-registers must carry the server's name
as a prefix, so that an agent connecting to multiple workbench servers
at once doesn't see name collisions in its MAF / SK / MCP tool list.

Prior to the fix that introduced this test, ``check_job`` was registered
without a prefix — three servers connected together produced three
``check_job`` entries and MAF refused to assemble the tool list.
"""

from __future__ import annotations

import asyncio

from .. import CodeExecutionServer, EnvironmentConfig
from ..auth import create_noop_auth_config


def _make_server(name: str) -> CodeExecutionServer:
    return CodeExecutionServer(
        environment_config=EnvironmentConfig(
            name=name,
            description="d",
            type="uv",
            dependency_file="numpy\n",
            auto_build=False,
        ),
        auth_config=create_noop_auth_config(),
    )


def _list_tool_names(server: CodeExecutionServer) -> list[str]:
    tools = asyncio.get_event_loop().run_until_complete(server.mcp.list_tools())
    return [t.name for t in tools]


class TestMetaToolPrefixing:
    def test_check_job_carries_server_prefix(self):
        names = _list_tool_names(_make_server("alpha"))
        assert "alpha_check_job" in names, f"check_job must be prefixed with server name; got: {sorted(names)}"
        assert "check_job" not in names, (
            "bare 'check_job' (no prefix) leaks across servers and breaks multi-server agent assembly"
        )

    def test_no_name_collisions_between_two_servers(self):
        """The tool lists of two servers must be disjoint after prefixing."""
        a = set(_list_tool_names(_make_server("alpha")))
        b = set(_list_tool_names(_make_server("beta")))
        overlap = a & b
        assert overlap == set(), (
            f"Tool names overlap between servers — would cause duplicate-tool errors in multi-server agents: {overlap}"
        )
