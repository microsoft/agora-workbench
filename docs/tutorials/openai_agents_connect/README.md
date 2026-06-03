# OpenAI Agents SDK — connect to an Agora Workbench server

A minimal walkthrough of **just the connection plumbing** between an
[`openai-agents`](https://github.com/openai/openai-agents-python) SDK
agent and a running Agora Workbench MCP server. The focus is the wiring
between the SDK and the workbench; build the agent itself by following
the [OpenAI Agents SDK docs](https://openai.github.io/openai-agents-python/).

## What you'll do

1. Start the chemistry MCP server locally on port 8020.
2. Wrap it in an `MCPServerStreamableHttp`.
3. Hand it to whatever `agents.Agent` you build.

```
┌────────────────────────────┐    Streamable HTTP    ┌──────────────────────────┐
│  your agents.Agent         │ ────────────────────► │  chemistry MCP server    │
│  (MCPServerStreamableHttp) │   /mcp + Bearer       │  127.0.0.1:8020/mcp      │
└────────────────────────────┘                       └──────────────────────────┘
```

## Prerequisites

- [`uv`](https://github.com/astral-sh/uv) installed.
- Docker — the chemistry server runs as a local container.
- `openai-agents` installed:
  ```bash
  uv sync --extra openai-agents
  ```

## Start the chemistry MCP server

### 1. Build the shared MCP base image (once)

```bash
docker build -f deployment/base.Dockerfile -t mcp-server-base:local .
```

### 2. Bring the server up

> **⚠️ Local-dev auth only** — the bundled server accepts any bearer
> token and binds to `127.0.0.1` only. Do not deploy this configuration.

```bash
cd examples/domain_examples/chemistry
docker compose up -d --build
curl http://localhost:8020/health
# => {"status":"healthy", ...}
```

## The OpenAI Agents SDK integration point

Wrap the running server in an `MCPServerStreamableHttp` and pass it to
whatever agent you build:

```python
from agents import Agent, Runner
from agents.mcp import MCPServerStreamableHttp

server = MCPServerStreamableHttp(
    name="chemistry",
    params={
        "url": "http://localhost:8020/mcp",
        "headers": {"Authorization": "Bearer dev-token"},
    },
    cache_tools_list=True,
)

# Build your agent however the OpenAI Agents SDK docs describe.
agent = Agent(
    name="my_agent",
    model=model,
    mcp_servers=[server],
    instructions="...",
)

async with server:
    result = await Runner.run(agent, "What does aspirin look like?")
```

Key options:

| Argument | What it does |
| --- | --- |
| `params["url"]` | Streamable-HTTP MCP endpoint — always ends in `/mcp`. |
| `params["headers"]` | Set the `Authorization` header here. |
| `cache_tools_list=True` | Skip re-listing tools on every turn — recommended once you've confirmed the server's tool set. |

`async with server:` opens the underlying MCP session. The Runner
re-uses it for the entire agent run and closes it on exit.

## Where to go next

- **Build the agent itself**: follow the
  [OpenAI Agents SDK docs](https://openai.github.io/openai-agents-python/) —
  pick a model, write instructions, call
  `Agent(mcp_servers=[server])`. The `server` is exactly the
  `MCPServerStreamableHttp` you built here.
- **Inject the workbench skill**: the
  [workbench runtime skill](../../../skills/agora-workbench/SKILL.md)
  is a portable system-prompt block that teaches any agent how to use
  Workbench tools correctly. Append it to your agent's `instructions`.
- **Authenticate against a real server**: swap the dev bearer for a real
  token — see [Authentication](../../guide/authentication.md).

## Cleanup

```bash
cd examples/domain_examples/chemistry && docker compose down
```
