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
| Build my first server | [Options for making a CodeExecutionServer](guide/server-options.md) |
| Add domain tools | [Tool pattern](guide/tool-pattern.md) |
| Add multi-step skills | [Skill pattern](guide/skill-pattern.md) |
| Write effective tools and skills | [Writing effective tools and skills](guide/writing-effective-tools.md) |
| Work with data files | [Working with data](guide/working-with-data.md) |
| Connect multiple servers | [Server networks](guide/server-networks.md) |
| Deploy to production | [Deploying your server](guide/deploying.md) |
| Monitor in production | [Monitoring your servers](guide/monitoring.md) |
| Configure authentication | [Authentication options](guide/authentication.md) |
| Extend with custom interfaces | [Extension points](guide/extension-points.md) |
