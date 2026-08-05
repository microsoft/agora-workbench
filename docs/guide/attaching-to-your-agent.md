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

The repo includes a ready-made
**[workbench runtime skill](https://github.com/microsoft/agora-workbench/blob/main/src/agora_workbench/skills/agora-workbench/SKILL.md)**
that you can inject into your agent's system prompt (or load via the
[Agent Skills](https://agentskills.io) standard). It teaches your agent:

- How to discover tools and skills before using them
- That domain tools are Python functions called inside `execute_{server}_code`, not standalone MCP tools
- How to handle sessions, artifacts, workflow planning, and async execution

Include the skill in your agent's context to significantly improve its first-attempt success rate with Agora Workbench servers. The skill follows the [Agent Skills format](https://agentskills.io/specification) and includes nested sub-skills for advanced topics (artifacts, workflow planning, async execution) that load on demand.

### Installing the skill

The skill ships inside the `agora-workbench` package, so you do not need a
checkout of this repository to use it. Install it into your agent's skills
directory with:

```bash
pip install agora-workbench
agora-workbench-deploy skill --output-dir ~/.claude/skills
```

This writes the full skill tree (`SKILL.md` plus its nested sub-skills) to
`<output-dir>/agora-workbench/`:

```
~/.claude/skills/agora-workbench/
├── SKILL.md
└── skills/
    ├── artifacts/SKILL.md
    ├── async-execution/SKILL.md
    └── workflow-planning/SKILL.md
```

Point `--output-dir` at whichever directory your agent client loads skills from
(`~/.claude/skills` for Claude Code, `.github/skills` for a repo-scoped skill, or
any path your framework scans). The default is `./skills`.

Useful flags:

| Flag | Purpose |
| --- | --- |
| `--list` | List the skills bundled with the installed package. |
| `--name NAME` | Install a specific bundled skill (default: `agora-workbench`). |
| `--force` | Replace an existing skill directory. It is removed first, so files dropped in a newer version are not left behind — use this to upgrade after `pip install --upgrade`. |

Re-running the command with `--force` after upgrading the package refreshes the
installed copy, so keep the skill in sync with the workbench version your
servers run.

## What's next

Once your agent is connected:

- [Writing effective tools and skills](writing-effective-tools.md) — best practices for the tools your agent will call
- [Working with data](working-with-data.md) — how your agent discovers and accesses data files
- [Monitoring your servers](monitoring.md) — watch what your agent is doing via the activity UI
