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

    read, write, _ = await stack.enter_async_context(streamable_http_client(MCP_URL, http_client=http_client))

    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()

    # — Use: call tools on the running server —
    tools = await session.list_tools()
    print("Available tools:", [tool.name for tool in tools.tools])

    run = await session.call_tool(
        "execute_starter_code",
        {
            "description": "Create a DataFrame and compute a sum.",
            "code": ("import pandas as pd\ndf = pd.DataFrame({'x': [1, 2, 3]})\nprint(int(df['x'].sum()))"),
        },
    )
    payload = json.loads(first_text(run))
    print("Execution stdout:", payload["stdout"].strip())

    sessions = await session.call_tool("starter_list_sessions", {"summary_only": True})
    print("Sessions:", first_text(sessions))

    session_id = payload["session_id"]
    closed = await session.call_tool("starter_close_session", {"session_id": session_id})
    print("Close session result:", first_text(closed))

    # — Teardown: close session, transport, and HTTP client —
    await stack.aclose()


if __name__ == "__main__":
    asyncio.run(main())
