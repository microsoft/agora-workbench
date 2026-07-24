# GitHub Copilot SDK — connect to an Agora Workbench server

A minimal walkthrough of **just the connection plumbing** between a
[`github-copilot-sdk`](https://pypi.org/project/github-copilot-sdk/)
session and a running Agora Workbench MCP server. The focus is where
the workbench slots into the Copilot session config; build the session
itself by following the [Copilot SDK docs](https://github.com/github/copilot-sdk/blob/main/python/README.md).

> **Why this looks different from the MAF / OpenAI Agents versions.**
> With the Copilot SDK there is no client-side agent object and no MCP
> client wrapper — the CLI *is* the agent and manages MCP transports
> internally. The integration point is a **config dict** you hand to
> `create_session(...)`.

## What you'll do

1. Start the chemistry MCP server locally on port 8020.
2. Pass an `mcp_servers={...}` block to `CopilotClient.create_session(...)`.

```
┌────────────────────────────┐                       ┌──────────────────────────┐
│  CopilotClient session     │   Streamable HTTP     │  chemistry MCP server    │
│  mcp_servers={...}         │ ────────────────────► │  127.0.0.1:8020/mcp      │
└────────────────────────────┘                       └──────────────────────────┘
```

## Prerequisites

- [`uv`](https://github.com/astral-sh/uv) installed.
- Docker — the chemistry server runs as a local container.
- `github-copilot-sdk` installed:
  ```bash
  uv sync --extra copilot-sdk
  ```
- Copilot auth set up (for when you actually run a session):
  ```bash
  copilot auth login
  ```

## Start the chemistry MCP server

Follow the steps in [Start the chemistry MCP server](../start-chemistry-server.md)
to build the base image and bring the server up at `http://localhost:8020/mcp`.

## The Copilot SDK integration point

Pass the running server as an `mcp_servers` config block when you
create a session:

```python
from copilot import CopilotClient, PermissionHandler

mcp_servers = {
    "chemistry": {
        "type": "http",
        "url": "http://localhost:8020/mcp",
        "headers": {"Authorization": "Bearer dev-token"},
        "tools": ["*"],  # allowlist; "*" enables every tool the server advertises
    },
}

async with CopilotClient() as client:
    async with await client.create_session(
        on_permission_request=PermissionHandler.approve_all,
        model="gpt-5",
        mcp_servers=mcp_servers,
        system_message={"mode": "append", "content": "..."},
    ) as session:
        reply = await session.send_and_wait("What does aspirin look like?")
```

Key fields of each `mcp_servers` entry (`MCPHTTPServerConfig`):

| Field | What it does |
| --- | --- |
| `type` | Transport — `"http"` for Workbench (Streamable HTTP at `/mcp`). |
| `url` | Streamable-HTTP MCP endpoint — always ends in `/mcp`. |
| `headers` | Set the `Authorization` header here. |
| `tools` | Allowlist of tool names. `["*"]` exposes everything the server registers. |

Other notes:

- `on_permission_request` is **required** on `create_session()`.
  `PermissionHandler.approve_all` waves through every tool call — fine
  for local dev, swap for a custom handler in production.
- `system_message={"mode": "append", ...}` (`SystemMessageAppendConfig`)
  appends to the Copilot CLI's built-in instructions instead of
  replacing them — recommended so you keep the SDK's safety guardrails.

## Where to go next

- **Build the session itself**: follow the
  [Copilot SDK docs](https://github.com/github/copilot-sdk/blob/main/python/README.md) for model
  selection, BYOK (Bring Your Own Key — supply a third-party LLM API key so the Copilot SDK routes
  requests through your own provider instead of the default Copilot backend) providers, streaming,
  and multi-turn chat. The
  `mcp_servers` block is exactly the one shown above.
- **Inject the workbench skill**: the
  [workbench runtime skill](https://github.com/microsoft/agora-workbench/blob/main/skills/agora-workbench/SKILL.md)
  is a portable system-prompt block that teaches any agent how to use
  Workbench tools correctly. Pass it as the `system_message` content
  (with `mode="append"`).
- **Authenticate against a real server**: swap the dev bearer for a real
  token — see [Authentication](../../guide/authentication.md).

## Cleanup

See [Cleanup](../start-chemistry-server.md#cleanup) in the shared server guide.
