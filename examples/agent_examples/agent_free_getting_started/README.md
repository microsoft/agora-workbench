# Agent-free CodeExecutionServer quickstart

This example shows how to run a `CodeExecutionServer` and call it directly over MCP (no agent framework required).

## 1) Start a server with a custom environment config

Create `server.py`:

```python
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
    # Custom environment location (environment config)
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
```

Run it:

```bash
uv run python server.py
```

## 2) Connect with a standard MCP Python client

Create `client_mcp_sdk.py`:

```python
import asyncio
import json

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

MCP_URL = "http://127.0.0.1:8010/mcp"


def first_text(result) -> str:
    for part in result.content:
        text = getattr(part, "text", None)
        if text:
            return text
    raise ValueError("No text content in MCP tool result")


async def main() -> None:
    timeout = httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)
    async with httpx.AsyncClient(
        headers={"Authorization": "Bearer dev-token"},
        timeout=timeout,
    ) as http_client:
        async with streamable_http_client(MCP_URL, http_client=http_client) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()

                tools = await session.list_tools()
                print("Available tools:", [tool.name for tool in tools.tools])

                run = await session.call_tool(
                    "execute_starter_code",
                    {
                        "description": "Create a DataFrame and compute a sum.",
                        "code": (
                            "import pandas as pd\n"
                            "df = pd.DataFrame({'x': [1, 2, 3]})\n"
                            "print(int(df['x'].sum()))"
                        ),
                    },
                )
                payload = json.loads(first_text(run))
                print("Execution stdout:", payload["stdout"].strip())

                sessions = await session.call_tool("starter_list_sessions", {"summary_only": True})
                print("Sessions:", first_text(sessions))

                session_id = payload["session_id"]
                closed = await session.call_tool("starter_close_session", {"session_id": session_id})
                print("Close session result:", first_text(closed))


if __name__ == "__main__":
    asyncio.run(main())
```

Run it while the server is running:

```bash
uv run python client_mcp_sdk.py
```

## 3) Connect with `curl` over HTTP

Initialize MCP and capture the `Mcp-Session-Id` response header:

```bash
INIT_HEADERS=/tmp/mcp-init-headers.txt

curl -sS -D "$INIT_HEADERS" http://127.0.0.1:8010/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'Authorization: Bearer dev-token' \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
      "protocolVersion": "2025-03-26",
      "capabilities": {},
      "clientInfo": {"name": "curl", "version": "1.0"}
    }
  }'

SESSION_ID=$(awk 'tolower($1)=="mcp-session-id:" {print $2}' "$INIT_HEADERS" | tr -d '\r')
echo "MCP session: $SESSION_ID"
```

List tools:

```bash
curl -sS http://127.0.0.1:8010/mcp \
  -H 'Content-Type: application/json' \
  -H "Mcp-Session-Id: $SESSION_ID" \
  -H 'Authorization: Bearer dev-token' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
```

Execute code:

```bash
curl -sS http://127.0.0.1:8010/mcp \
  -H 'Content-Type: application/json' \
  -H "Mcp-Session-Id: $SESSION_ID" \
  -H 'Authorization: Bearer dev-token' \
  -d '{
    "jsonrpc":"2.0",
    "id":3,
    "method":"tools/call",
    "params":{
      "name":"execute_starter_code",
      "arguments":{
        "description":"Compute 2+2",
        "code":"print(2+2)"
      }
    }
  }'
```

Close the session:

```bash
curl -sS http://127.0.0.1:8010/mcp \
  -H 'Content-Type: application/json' \
  -H "Mcp-Session-Id: $SESSION_ID" \
  -H 'Authorization: Bearer dev-token' \
  -d "{
    \"jsonrpc\":\"2.0\",
    \"id\":4,
    \"method\":\"tools/call\",
    \"params\":{
      \"name\":\"starter_close_session\",
      \"arguments\":{\"session_id\":\"$SESSION_ID\"}
    }
  }"
```

> `create_noop_auth_config()` is for local development only. For deployed endpoints use Entra auth (`create_entra_auth_config()`).
