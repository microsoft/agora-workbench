# Agora Workbench

API documentation for the Agora multi-domain AI agent system — combining LLM-driven
workflows with isolated code execution environments.

## Overview

Agora Workbench provides:

- **MCP Code Execution Servers** — isolated Python environments with domain-specific packages
- **Session Management** — stateful, per-user sessions with lifecycle management
- **Tool Registry** — dynamic tool discovery, registration, and proxy generation
- **Data Access** — asset resolution and provisioning from Azure Data Lake
- **Authentication** — pluggable auth via Entra ID / JWT tokens

## Quick Links

| Module | Description |
|--------|-------------|
| [`CodeExecutionServer`](api/server.md) | Base server class for MCP code execution |
| [`Models`](api/models.md) | Pydantic data models (configs, results, specs) |
| [`Sessions`](api/sessions.md) | Session lifecycle and storage |
| [`Tool Registry`](api/tool_registry.md) | Tool schemas, registration, and search |
| [`Data Access`](api/data_access.md) | Asset resolution and data lake integration |
| [`Auth`](api/auth.md) | Authentication backends |

## Building the Docs

```bash
uv run mkdocs serve   # live preview at http://127.0.0.1:8000
uv run mkdocs build   # static site in site/
```
