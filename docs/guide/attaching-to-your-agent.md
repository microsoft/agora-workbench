# Attaching to your agent

Agora Workbench servers are framework-agnostic MCP servers — any agent framework that supports MCP's Streamable HTTP transport can connect to them. This page points you to the tutorials that demonstrate the integration for each supported framework.

## Connection tutorials

Each tutorial below is a minimal walkthrough of the connection plumbing — wire your existing agent to a running Workbench MCP server. Build the agent itself by following your framework's own docs.

| Framework | Tutorial |
|-----------|----------|
| **Microsoft Agent Framework (MAF)** | [MAF Connect](../tutorials/maf_connect/README.md) |
| **OpenAI Agents SDK** | [OpenAI Agents Connect](../tutorials/openai_agents_connect/README.md) |
| **GitHub Copilot SDK** | [Copilot SDK Connect](../tutorials/copilot_sdk_connect/README.md) |

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

For a minimal example without any agent framework (raw MCP client or `curl`), see [`examples/agent_free_getting_started/README.md`](https://github.com/microsoft/agora-workbench/tree/main/examples/agent_free_getting_started).

## Workbench skill

The repo includes a ready-made **[workbench runtime skill](../../skills/agora-workbench/SKILL.md)** that you can inject into your agent's system prompt (or load via the [Agent Skills](https://agentskills.io) standard). It teaches your agent:

- How to discover tools and skills before using them
- That domain tools are Python functions called inside `execute_{server}_code`, not standalone MCP tools
- How to handle sessions, artifacts, workflow planning, and async execution

Include the skill in your agent's context to significantly improve its first-attempt success rate with Agora Workbench servers. The skill follows the [Agent Skills format](https://agentskills.io/specification) and includes nested sub-skills for advanced topics (artifacts, workflow planning, async execution) that load on demand.

## What's next

Once your agent is connected:

- [Writing effective tools and skills](writing-effective-tools.md) — best practices for the tools your agent will call
- [Working with data](working-with-data.md) — how your agent discovers and accesses data files
- [Monitoring your servers](monitoring.md) — watch what your agent is doing via the activity UI
