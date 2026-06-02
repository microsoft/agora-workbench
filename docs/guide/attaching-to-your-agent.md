# Attaching to your agent

Agora Workbench servers are framework-agnostic MCP servers — any agent framework that supports MCP's Streamable HTTP transport can connect to them. This page points you to the tutorials that demonstrate the integration for each supported framework.

## Supported frameworks

| Framework | Tutorial | Description |
|-----------|----------|-------------|
| **Microsoft Agent Framework (MAF)** | [MAF Quickstart](../tutorials/maf_quickstart/README.md) | Wire a MAF agent to two domain MCP servers (chemistry + energy systems) using `MCPStreamableHTTPTool` |
| **OpenAI Agents SDK** | [OpenAI Agents Quickstart](../tutorials/openai_agents_quickstart/README.md) | Wire an OpenAI Agents SDK agent to the chemistry MCP server using `MCPServerStreamableHttp` |
| **GitHub Copilot SDK** | [Copilot SDK Quickstart](../tutorials/copilot_sdk_quickstart/README.md) | Connect a `CopilotClient` session to the energy systems MCP server |

## How it works

From the agent's perspective, connecting to an Agora Workbench server means:

1. **Point your MCP client at the server's `/mcp` endpoint** (e.g., `http://localhost:8000/mcp`)
2. **The agent discovers tools automatically** — `execute_{name}_code`, `search_{name}_tools`, session management, etc.
3. **Call tools through code execution** — the agent writes Python that invokes domain tools inside the server's sandboxed environment

No Agora-specific client library is needed. Any MCP-compatible client works.

## Bring your own agent (BYOA)

If your framework isn't listed above, you can connect any MCP client that supports Streamable HTTP. The key points:

- **Transport**: Streamable HTTP at `http://<host>:<port>/mcp`
- **Auth**: Bearer token in the `Authorization` header (or no auth with `create_noop_auth_config()` for local dev)
- **Tools**: Auto-discovered — the agent receives all registered tools on connection

For a minimal example without any agent framework (raw MCP client or `curl`), see [`examples/agent_examples/agent_free_getting_started/README.md`](https://github.com/microsoft/agora-workbench/tree/main/examples/agent_examples/agent_free_getting_started).

## What's next

Once your agent is connected:

- [Writing effective tools and skills](writing-effective-tools.md) — best practices for the tools your agent will call
- [Working with data](working-with-data.md) — how your agent discovers and accesses data files
- [Monitoring your servers](monitoring.md) — watch what your agent is doing via the activity UI
