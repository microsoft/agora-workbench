# MAF — connect to an Agora Workbench server

A minimal walkthrough of **just the connection plumbing** between a
[Microsoft Agent Framework](https://github.com/microsoft/agent-framework)
agent and a running Agora Workbench MCP server. The focus is the wiring
between MAF and the workbench; build the agent itself by following the
[MAF docs](https://github.com/microsoft/agent-framework).

## What you'll do

1. Start the chemistry MCP server locally on port 8020.
2. Wrap it in a MAF `MCPStreamableHTTPTool`.
3. Hand the tool to whatever MAF agent you build.

```
┌────────────────────────┐    Streamable HTTP    ┌──────────────────────────┐
│  your MAF agent        │ ────────────────────► │  chemistry MCP server    │
│  (MCPStreamableHTTPTool)│   /mcp + Bearer       │  127.0.0.1:8020/mcp      │
└────────────────────────┘                       └──────────────────────────┘
```

## Prerequisites

- Python 3.11 or later.
- Docker — the chemistry server runs as a local container.
- Agora Workbench and Microsoft Agent Framework installed from PyPI:
  ```bash
  python -m pip install "agora-workbench==0.2.0" agent-framework
  ```

## Start the chemistry MCP server

Follow the steps in [Start the chemistry MCP server](../start-chemistry-server.md)
to build the base image and bring the server up at `http://localhost:8020/mcp`.

## The MAF integration point

Wrap the running server in an `MCPStreamableHTTPTool` and pass it to
whatever agent you build:

```python
import httpx
from agent_framework import MCPStreamableHTTPTool

tool = MCPStreamableHTTPTool(
    name="chemistry",
    url="http://localhost:8020/mcp",
    http_client=httpx.AsyncClient(
        headers={"Authorization": "Bearer dev-token"},
    ),
    tool_name_prefix="chem_",
    approval_mode="never_require",
)

# Build your agent however MAF documents — see links below.
agent = chat_client.as_agent(name="my_agent", tools=[tool], instructions="...")

async with tool:
    await agent.run("What does aspirin look like?")
```

Key options:

| Argument | What it does |
| --- | --- |
| `url` | Streamable-HTTP MCP endpoint — always ends in `/mcp`. |
| `http_client` | Your own `httpx.AsyncClient`. Set the `Authorization` header here. |
| `tool_name_prefix` | Optional namespacing — every advertised tool is exposed to the LLM as `<prefix><tool>`. Use this when you attach multiple Workbench servers. |
| `approval_mode` | `"never_require"` runs tools without per-call confirmation prompts. |

`async with tool:` opens the underlying MCP session. MAF re-uses it for
the entire agent run and closes it on exit.

## Where to go next

- **Build the agent itself**: follow the
  [MAF samples](https://github.com/microsoft/agent-framework/tree/main/python/samples) —
  pick a chat client, write instructions, call
  `chat_client.as_agent(tools=[tool])`. The `tool` is exactly the
  `MCPStreamableHTTPTool` you built here.
- **Inject the workbench skill**: the
  [workbench runtime skill](https://github.com/microsoft/agora-workbench/blob/main/src/agora_workbench/skills/agora-workbench/SKILL.md)
  is a portable system-prompt block that teaches any agent how to use
  Workbench tools correctly. Append it to your MAF agent's
  `instructions`. Get a local copy with
  `agora-workbench-deploy skill --output-dir ./skills`.
- **Authenticate against a real server**: swap the dev bearer for a real
  token — see [Authentication](../../guide/authentication.md).

## Cleanup

See [Cleanup](../start-chemistry-server.md#cleanup) in the shared server guide.
