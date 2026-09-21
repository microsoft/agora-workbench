"""Security regressions for unauthenticated HTTP bind addresses."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agora_workbench.code_execution import CodeExecutionServer, ServerConfig
from agora_workbench.code_execution.auth import AuthConfig, IdentityExtractor, TokenValidator, create_noop_auth_config


class _StrictTokenValidator(TokenValidator):
    async def validate(self, token: str, *, request_path: str = "/mcp", request_method: str = "POST") -> dict:
        return {"sub": "authenticated-user"}


class _StrictIdentityExtractor(IdentityExtractor):
    def extract(self, claims: dict) -> str:
        return str(claims["sub"])


def _make_server(auth_config: AuthConfig | None = None) -> CodeExecutionServer:
    config = ServerConfig(
        name="test_http_binding",
        description="Test HTTP binding security",
        type="uv",
        dependency_file="# empty",
    )
    return CodeExecutionServer(
        server_config=config,
        auth_config=auth_config or create_noop_auth_config(),
    )


async def _run_without_network(
    server: CodeExecutionServer,
    *,
    host: str | None = None,
    allow_unauthenticated_remote: bool = False,
) -> None:
    server._startup = AsyncMock()
    app = MagicMock()
    app.routes = []
    with (
        patch.object(server.mcp, "http_app", return_value=app),
        patch("uvicorn.Server.serve", new_callable=AsyncMock),
    ):
        if host is None:
            await server.run_http(allow_unauthenticated_remote=allow_unauthenticated_remote)
        else:
            await server.run_http(
                host=host,
                allow_unauthenticated_remote=allow_unauthenticated_remote,
            )


@pytest.mark.unit
@pytest.mark.parametrize("host", ["0.0.0.0", "::", "10.0.0.5", "server.internal", ""])
async def test_noop_auth_rejects_non_loopback_bind_before_startup(host: str):
    server = _make_server()
    server._startup = AsyncMock()

    with pytest.raises(ValueError, match="Refusing to bind an unauthenticated MCP server"):
        await server.run_http(host=host, port=9999)

    server._startup.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1", "[::1]", "localhost"])
async def test_noop_auth_allows_loopback_bind(host: str):
    server = _make_server()

    await _run_without_network(server, host=host)

    assert server._bind_host == host


@pytest.mark.unit
async def test_noop_auth_defaults_to_loopback():
    server = _make_server()

    await _run_without_network(server)

    assert server._bind_host == "127.0.0.1"


@pytest.mark.unit
async def test_explicit_acknowledgement_allows_unauthenticated_remote_bind(caplog):
    server = _make_server()

    await _run_without_network(
        server,
        host="0.0.0.0",
        allow_unauthenticated_remote=True,
    )

    assert server._bind_host == "0.0.0.0"
    assert "explicit insecure-network acknowledgement" in caplog.text


@pytest.mark.unit
async def test_environment_acknowledgement_allows_direct_run_http_call(caplog):
    server = _make_server()

    with patch.dict("os.environ", {"AGORA_ALLOW_UNAUTHENTICATED_REMOTE": "1"}):
        await _run_without_network(server, host="0.0.0.0")

    assert server._bind_host == "0.0.0.0"
    assert "explicit insecure-network acknowledgement" in caplog.text


@pytest.mark.unit
async def test_invalid_environment_acknowledgement_rejects_direct_run_http_call():
    server = _make_server()
    server._startup = AsyncMock()

    with (
        patch.dict("os.environ", {"AGORA_ALLOW_UNAUTHENTICATED_REMOTE": "maybe"}),
        pytest.raises(ValueError, match="Invalid AGORA_ALLOW_UNAUTHENTICATED_REMOTE"),
    ):
        await server.run_http(host="0.0.0.0", port=9999)

    server._startup.assert_not_awaited()


@pytest.mark.unit
async def test_authenticated_server_allows_remote_bind_without_acknowledgement():
    auth_config = AuthConfig(
        token_validator=_StrictTokenValidator(),
        identity_extractor=_StrictIdentityExtractor(),
        require_authorization_header=True,
    )
    server = _make_server(auth_config)

    await _run_without_network(server, host="0.0.0.0")

    assert server._bind_host == "0.0.0.0"


@pytest.mark.unit
async def test_noop_validator_marker_rejects_remote_bind_even_if_header_is_required():
    auth_config = create_noop_auth_config()
    auth_config.require_authorization_header = True
    server = _make_server(auth_config)
    server._startup = AsyncMock()

    with pytest.raises(ValueError, match="Refusing to bind an unauthenticated MCP server"):
        await server.run_http(host="0.0.0.0", port=9999)

    server._startup.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.parametrize(
    "relative_path",
    [
        "docs/tutorials/first_server/README.md",
        "docs/guide/server-options.md",
        "src/agora_workbench/code_execution/README.md",
    ],
)
def test_noop_getting_started_docs_do_not_bind_all_interfaces(relative_path: str):
    repo_root = Path(__file__).resolve().parents[4]
    content = (repo_root / relative_path).read_text(encoding="utf-8")

    assert 'run_http(host="0.0.0.0"' not in content
