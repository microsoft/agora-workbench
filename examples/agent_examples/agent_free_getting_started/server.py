import asyncio
from pathlib import Path

from code_execution import CodeExecutionServer, ServerConfig
from code_execution.auth import create_noop_auth_config

config = ServerConfig(
    name="starter",
    description="Minimal MCP Python execution server with numpy and pandas.",
    type="uv",
    dependency_file="numpy>=2.0.0\npandas>=2.3.0\n",
    auto_build=True,
    # environment location for local installation
    build_dir=Path.home() / ".cache" / "mcp-envs" / "starter-demo" / "uv",
)

server = CodeExecutionServer(
    server_config=config,
    auth_config=create_noop_auth_config(),  # local-dev only
)


async def main() -> None:
    await server.run_http(host="127.0.0.1", port=8010)


if __name__ == "__main__":
    asyncio.run(main())
