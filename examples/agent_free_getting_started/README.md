# Agent-free CodeExecutionServer quickstart

This example shows how to run a `CodeExecutionServer` and call it directly over MCP (no agent framework required).

## 1) Start a server with a custom environment config

Create `server.py`:

```python
import asyncio
from pathlib import Path

from agora_workbench.code_execution import CodeExecutionServer, ServerConfig
from agora_workbench.code_execution.auth import create_noop_auth_config

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
from contextlib import AsyncExitStack

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
    # — Setup: open HTTP client, transport, and MCP session —
    stack = AsyncExitStack()

    timeout = httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)
    http_client = httpx.AsyncClient(
        headers={"Authorization": "Bearer dev-token"},
        timeout=timeout,
    )
    await stack.enter_async_context(http_client)

    read, write, _ = await stack.enter_async_context(
        streamable_http_client(MCP_URL, http_client=http_client)
    )

    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()

    # — Use: call tools on the running server —
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
    resumed = await session.call_tool(
        "execute_starter_code",
        {
            "description": "Reuse the retained DataFrame.",
            "execution_session_id": session_id,
            "code": "print(int(df['x'].max()))",
        },
    )
    print("Resumed execution stdout:", json.loads(first_text(resumed))["stdout"].strip())

    closed = await session.call_tool("starter_close_session", {"session_id": session_id})
    print("Close session result:", first_text(closed))

    # — Teardown: close session, transport, and HTTP client —
    await stack.aclose()


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

Execute code and capture the execution `session_id` from the response:

```bash
EXEC_RESPONSE=$(curl -sS http://127.0.0.1:8010/mcp \
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
        "code":"value = 2 + 2\nprint(value)"
      }
    }
  }')

echo "$EXEC_RESPONSE"
EXEC_SESSION_ID=$(echo "$EXEC_RESPONSE" | python3 -c "import sys,json; r=json.load(sys.stdin); print(json.loads(r['result']['content'][0]['text'])['session_id'])")
echo "Execution session: $EXEC_SESSION_ID"
```

Resume that execution session from this or another authenticated MCP connection
by passing its ID to the execution tool. This ID is not the `Mcp-Session-Id`
transport header:

```bash
curl -sS http://127.0.0.1:8010/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H "Mcp-Session-Id: $SESSION_ID" \
  -H 'Authorization: ******' \
  -d "{
    \"jsonrpc\":\"2.0\",
    \"id\":4,
    \"method\":\"tools/call\",
    \"params\":{
      \"name\":\"execute_starter_code\",
      \"arguments\":{
        \"description\":\"Reuse state from the retained execution session\",
        \"execution_session_id\":\"$EXEC_SESSION_ID\",
        \"code\":\"print(value)\"
      }
    }
  }"
```

Close the execution session (note: this is the execution session ID, not the MCP transport session):

```bash
curl -sS http://127.0.0.1:8010/mcp \
  -H 'Content-Type: application/json' \
  -H "Mcp-Session-Id: $SESSION_ID" \
  -H 'Authorization: Bearer dev-token' \
  -d "{
    \"jsonrpc\":\"2.0\",
    \"id\":5,
    \"method\":\"tools/call\",
    \"params\":{
      \"name\":\"starter_close_session\",
      \"arguments\":{\"session_id\":\"$EXEC_SESSION_ID\"}
    }
  }"
```

> `create_noop_auth_config()` is for local development only. For deployed endpoints use Entra auth (`create_entra_auth_config()`).
