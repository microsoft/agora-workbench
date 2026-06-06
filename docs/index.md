# Agora Workbench

A multi-domain AI agent system that combines LLM-driven workflows with isolated code execution environments.

## What is Agora Workbench?

Agora Workbench provides **`CodeExecutionServer`** — a base class for building MCP (Model Context Protocol) servers that execute Python code in sandboxed environments with domain-specific packages. An AI agent connects to your server, discovers available tools, and runs code against them.

Key capabilities:

- **Isolated code execution** — each server runs Python in its own environment (uv, conda, or pip) with its own dependencies
- **Domain tools & skills** — register typed tool definitions and multi-step skill workflows that the agent can discover and invoke
- **Data access** — server-side file catalog with hybrid keyword + vector search over local and blob storage artifacts
- **Authentication** — pluggable auth with built-in support for Azure Entra ID and no-op dev mode
- **Server networks** — compose multiple servers behind a `ConnectorServer` (router or gateway) for multi-domain deployments
- **Deployment** — Docker-based deployment with Azure Container Apps support

## Quick links

| I want to… | Go to… |
|---|---|
| Build my first server | [Your First Server tutorial](tutorials/first_server/README.md) · [All server options](guide/server-options.md) |
| Add domain tools | [Tool pattern](guide/tool-pattern.md) |
| Add multi-step skills | [Skill pattern](guide/skill-pattern.md) |
| Teach my agent how to use the servers | [Attaching to your agent](guide/attaching-to-your-agent.md#workbench-skill) |
| Write effective tools and skills | [Writing effective tools and skills](guide/writing-effective-tools.md) |
| Work with data files | [Working with data](guide/working-with-data.md) |
| Connect multiple servers | [Server networks](guide/server-networks.md) |
| Deploy to production | [Deploying your server](guide/deploying.md) |
| Monitor in production | [Monitoring your servers](guide/monitoring.md) |
| Configure authentication | [Authentication options](guide/authentication.md) |
| Extend with custom interfaces | [Extension points](guide/extension-points.md) |

## Tutorials

Minimal connection walkthroughs for each supported SDK — wire your
existing agent to a running Workbench MCP server in a few lines.

**Before running any tutorial**, follow the
[Start the chemistry MCP server](tutorials/start-chemistry-server.md) guide to
build the base image and start the local server.

| Tutorial | What you'll learn |
| --- | --- |
| [Your First Server](tutorials/first_server/README.md) | Build a server with a real domain tool, end-to-end |
| [Microsoft Agent Framework (MAF)](tutorials/maf_connect/README.md) | Connect a MAF agent to a running Workbench server |
| [OpenAI Agents SDK](tutorials/openai_agents_connect/README.md) | Connect an OpenAI agent to a running Workbench server |
| [GitHub Copilot SDK](tutorials/copilot_sdk_connect/README.md) | Connect a Copilot agent to a running Workbench server |

## Source

Code, issues, and contributions: <https://github.com/microsoft/agora-workbench>.
