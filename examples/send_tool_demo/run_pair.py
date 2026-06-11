"""
Unified send tool demo — launches two lightweight CodeExecutionServers
("alpha" on port 8001, "beta" on port 8002) that can push objects to each
other via the `{name}_send` MCP tool.

Usage:
    uv run python examples/send_tool_demo/run_pair.py

Then connect an MCP client (e.g. Claude Desktop, mcp-cli) to either server
and exercise the send tool:

    # On alpha (http://localhost:8001/mcp):
    1. execute_alpha_code: x = {"hello": "from alpha"}
    2. alpha_send(data_ref="x", to="beta")
    3. On beta, the variable `x` is now available in the kernel.

    # On beta (http://localhost:8002/mcp):
    1. execute_beta_code: result = [1, 2, 3]
    2. beta_send(data_ref="result", to="alpha")
    3. On alpha, the variable `result` is now available.

    # Send to local filesystem:
    1. execute_alpha_code:
           import pandas as pd
           df = pd.DataFrame({"a": [1,2,3]})
           df.to_csv("/tmp/agora_output/report.csv", index=False)
    2. alpha_send(data_ref="report.csv", to="local")

    # Send to user (activity UI download):
    1. alpha_send(data_ref="report.csv", to="user")

Environment variables (optional):
    ALPHA_PORT  — port for alpha server (default: 8001)
    BETA_PORT   — port for beta server (default: 8002)
"""

import asyncio
import os
import sys
from pathlib import Path

from agora_workbench.code_execution import CodeExecutionServer, ServerConfig, ServerPublisher
from agora_workbench.code_execution.auth import create_noop_auth_config
from agora_workbench.code_execution.data_access import LocalFilePublisher

ALPHA_PORT = int(os.getenv("ALPHA_PORT", "8006"))
BETA_PORT = int(os.getenv("BETA_PORT", "8007"))

PUBLISH_DIR = Path(os.getenv("PUBLISH_DIR", "/tmp/send_tool_demo_artifacts"))

# Minimal requirements — dill is needed for object serialization in send.
REQUIREMENTS = "dill\n"


def make_server(name: str, port: int, peer_name: str, peer_port: int) -> CodeExecutionServer:
    """Create a CodeExecutionServer configured to send objects to a peer."""
    config = ServerConfig(
        name=name,
        description=f"Demo '{name}' server for testing the unified send tool.",
        type="uv",
        dependency_file=REQUIREMENTS,
        auto_build=True,
    )

    publishers = [
        # Peer server destination
        ServerPublisher(
            server_name=peer_name,
            target_url=f"http://localhost:{peer_port}",
        ),
        # Local filesystem destination
        LocalFilePublisher(base_dir=PUBLISH_DIR / name),
    ]

    return CodeExecutionServer(
        server_config=config,
        auth_config=create_noop_auth_config(),
        publishers=publishers,
    )


async def main():
    # Allow plain HTTP between localhost peers
    os.environ.setdefault("OBJECT_TRANSFER_TRUSTED_HTTP_HOSTS", "localhost")

    alpha = make_server("alpha", ALPHA_PORT, peer_name="beta", peer_port=BETA_PORT)
    beta = make_server("beta", BETA_PORT, peer_name="alpha", peer_port=ALPHA_PORT)

    print(f"Starting alpha on http://localhost:{ALPHA_PORT}/mcp")
    print(f"Starting beta  on http://localhost:{BETA_PORT}/mcp")
    print(f"Local publish dir: {PUBLISH_DIR}")
    print("\nPress Ctrl+C to stop both servers.\n")

    # Run both servers concurrently
    await asyncio.gather(
        alpha.run_http(host="0.0.0.0", port=ALPHA_PORT),
        beta.run_http(host="0.0.0.0", port=BETA_PORT),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down.")
        sys.exit(0)
