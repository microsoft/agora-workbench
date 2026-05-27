# Project

<!-- > This repo has been populated by an initial template to help get you started. Please
> make sure to update the content to build a great experience for community-building.

As the maintainer of this project, please make a few updates:

- Improving this README.MD file to provide a great experience
- Updating SUPPORT.MD with content about this project's support experience
- Understanding the security reporting process in SECURITY.MD
- Remove this section from the README -->

# Agora Agent

A multi-domain AI agent system that combines LLM-driven workflows with isolated code execution environments.

## Overview

Agora Agent uses MAF workflows with explicit executors, Pydantic-based structured outputs, and MCP (Model Context Protocol) servers that provide sandboxed Python execution with domain-specific packages.

Key capabilities:

- **MAF Workflows** — state graph with executors that maintain conversation history, iteration count, and other state internally
- **MCP Code Execution** — domain-specific Python environments served over MCP, each running in Docker with its own dependencies
- **Tool Discovery** — `search_tools` for natural-language catalog search; MCP server tools (`execute_code`, session management) are auto-discovered from `server_registry.yaml` at agent startup and available from the first turn
- **Context Management** — MAF-native compaction via `CompactionProvider` with token-budget-aware strategies (tool-result compaction, LLM summarization, sliding window)
- **Data Lake Integration** — server-side file catalog with hybrid keyword + vector search over local and blob storage artifacts

## Structure

```
src/
├── auth/               # Agent-side credentials (ChainedTokenCredential)
├── tools/              # Tool search, MCP server registry, tool catalog
│   ├── tool_descriptor.py      # ToolDescriptor — framework-agnostic callable + JSON Schema
│   ├── search/                 # search_tools backends (BM25, Azure AI Search)
│   │   ├── core.py             # create_search_tools_descriptor (no framework dep)
│   │   ├── state_graph_tools.py# create_query_state_graph_descriptor (no framework dep)
│   │   └── adapters/           # MAF FunctionTool wrappers (requires [maf] extra)
│   └── mcp/adapters/           # MAF MCPStreamableHTTPTool wrappers (requires [maf] extra)
├── code_execution/     # CodeExecutionServer base class, sessions, Docker config
│   ├── data_access/catalog/  # Server-side file catalog (SQLite + FTS5 + sqlite-vec)
│   └── deploy/         # Azure Container Apps deployment (Bicep + deploy script)
├── gui/                # GIS map GUI (FastAPI backend + React/Vite frontend)
├── server_registry.yaml    # MCP server configurations
└── pyproject.toml
```

All `*/adapters/` directories contain framework-specific bridges.  They are the **only** source files that import `agent_framework` and they are isolated behind the `[maf]` optional extra.  The rest of the codebase is framework-agnostic.

## Getting Started

### Prerequisites

- **Python 3.11+**
- **uv** package manager for dependency management
- **Git** with submodule support

### Installation

```bash
uv sync            # install base dependencies (framework-agnostic)
uv sync --group dev  # include dev tools (pytest, pre-commit, jupyter)
```

**Optional extras** — install only what you need:

| Extra | Command | What it adds |
|-------|---------|--------------|
| `maf` | `uv sync --extra maf` | `agent-framework` — required only to use the MAF adapters (`*/adapters/maf*.py`) that wrap tool descriptors in `FunctionTool` / `MCPStreamableHTTPTool` |

The base package ships the full framework-agnostic layer:
- `tools.tool_descriptor.ToolDescriptor` — callable + JSON Schema, usable with any agent framework
- `tools.search.core` / `tools.search.state_graph_tools` — descriptor factories for search and state-graph tools

If you are **not** using MAF, you never need `agent-framework`.  Write a one-liner adapter that converts a `ToolDescriptor` to whatever your framework accepts (callable + JSON Schema is the typical input).

Copy `.env.example` to `.env` and configure credentials. See the comments in that file for available settings.

For a minimal, **agent-free** MCP flow (standalone `CodeExecutionServer` + direct MCP client / `curl` calls), see [`examples/agent_examples/agent_free_getting_started/README.md`](examples/agent_examples/agent_free_getting_started/README.md).

## Development

```bash
# Run tests
uv run pytest                             # all configured test paths
uv run pytest src/code_execution/tests/   # code execution tests

# Lint and format
uv run pre-commit run --all-files
```

## Tool Discovery

Tool discovery uses a single agent-facing tool plus auto-discovered MCP server tools:

1. **`search_tools`** — searches the tool catalog by natural-language query and returns matching tool names, descriptions, server names, and relevance scores. Use this to discover which domain tools exist and what they do.
2. **MCP server tools** — `execute_code` and session management tools for each registered server are auto-discovered from `server_registry.yaml` at agent startup and passed directly to the executor. They are available from the first turn without any explicit loading step.

Domain tools are **not** exposed directly through the MCP interface. Instead, the agent invokes them programmatically by executing Python code via the server's `execute_code` tool. Use `search_tools` to discover a tool's name, signature, and which server it belongs to, then call it from within an `execute_code` block.

The search backend is pluggable via the `ToolSearchBackend` abstract base class (`tools/tool_search.py`):

| Backend | Class | When to use |
|---------|-------|-------------|
| **BM25** (default) | `BM25ToolSearchBackend` | Local, zero-dependency keyword search over the YAML-derived tool catalog. |
| **Azure AI Search** | `AzureAIToolSearchBackend` | Cloud-hosted semantic/vector search. Set `TOOL_SEARCH_ENDPOINT` in `.env`. |

## Authentication

### Agent-side credentials

`auth/auth.py` creates a `ChainedTokenCredential` that tries, in order:

1. `AzureCliCredential` — for local development (`az login`)
2. `ManagedIdentityCredential` — for deployed Azure resources

### MCP server OBO credentials

MCP code-execution servers use `OBOCredentialProvider` (`code_execution/auth/obo_credential.py`) to obtain tokens for downstream Azure resources. Three authentication modes are supported. The active mode is chosen in the following priority order based on constructor overrides and environment variables:

| Priority | Mode | When active | Description |
|:---:|------|-------------|-------------|
| 1 | **Simulation** | `simulation_mode=True` **or** `OBO_SIMULATION_MODE=true` | Uses the developer's `az login` credentials. For local development only — never enable in production. |
| 2 | **Managed Identity** | `managed_identity=True` **or** (`AZURE_CLIENT_ID` is set **and** `AZURE_FEDERATED_TOKEN_FILE` is not set) | Uses `ManagedIdentityCredential` to authenticate directly as the container's managed identity. Set `AZURE_CLIENT_ID` for user-assigned identity, or pass `managed_identity=True` to the constructor for system-assigned identity. |
| 3 | **Federated Token (OBO)** | Fallback when neither Simulation nor Managed Identity is active | Exchanges the user's assertion token via `OnBehalfOfCredential` with workload-identity federation. Requires `ENTRA_CLIENT_ID`, `ENTRA_TENANT_ID`, and `AZURE_FEDERATED_TOKEN_FILE`. |

## Deployment

Docker-based deployment for `CodeExecutionServer` instances. The shared base image and deployment tooling live in `src/deployment/mcp_server/`.

- **Local development** — build the base image, extend it with your server, and run with Docker Compose. See [`src/deployment/mcp_server/README.md`](src/deployment/mcp_server/README.md).
- **Azure Container Apps** — build, push, and deploy via Bicep. See [`src/deployment/mcp_server/container_apps/README.md`](src/deployment/mcp_server/container_apps/README.md).

## Code Execution

### Output Truncation

`CodeExecutionServer` truncates large `stdout`/`stderr` to prevent bloating the LLM context window. Truncation is enabled by default and configurable.

| Setting | Default | Description |
|---|---|---|
| `ServerConfig.output_truncation_threshold` | `50_000` | Maximum characters allowed per output stream before truncation is applied (that is, `stdout` and `stderr` are evaluated independently, not as a combined total). A guidance message is appended instructing the LLM to inspect large objects server-side. |
| Environment variable `CODE_OUTPUT_TRUNCATION_THRESHOLD` | _(unset)_ | Overrides the config value when set. Takes precedence. |

Set to `0` to **disable** truncation entirely (not recommended for production — very large outputs will be passed to the LLM unchanged).

```python
# Custom threshold via ServerConfig
config = ServerConfig(
    ...,
    output_truncation_threshold=100_000,  # 100 k chars
)
server = MyCodeExecutionServer(server_config=config)
```

```bash
# Override via environment variable
export CODE_OUTPUT_TRUNCATION_THRESHOLD=0     # disable truncation
export CODE_OUTPUT_TRUNCATION_THRESHOLD=25000 # 25 k chars
```

### Background jobs and polling

- `execute_<server>_code(..., background=True)` submits execution asynchronously and returns `{job_id, status, session_id}`.
- `check_job(job_id)` polls job state/output.
- Only one concurrent background job is allowed per session.

### Parallel batches and session inspection

- `<server>_parallel_execute` is available for map-style batch submission.
- `<server>_check_batch` and `<server>_cancel_batch` are currently affected by a known server-side ownership-check issue and may error; do not rely on batch status/results until that implementation is fixed.
- `<server>_inspect_session` returns namespace summaries and current job state.
- `PARALLEL_EXECUTE_MAX_CONCURRENCY` controls server-wide parallel-job concurrency (`0` disables the cap).
- Completed/cancelled batches are pruned from memory after final status is read.

### Session-limit policy

Session capacity is enforced as **rejection** (HTTP 429 with `error_type: max_sessions_reached`) rather than LRU eviction. Clients should close an existing session and retry.

For full details and payload shapes, see [`src/code_execution/README.md`](src/code_execution/README.md).

### GUI map capture

- GUI includes `capture_map_view` for visual map screenshots; frontend capture relies on `html2canvas`.

## Data Catalog

The data catalog is a server-side component that runs inside each MCP code execution server. It provides hybrid keyword + vector search over data files declared in a `catalog.yaml` configuration file.

### Configuration

Create a `catalog.yaml` in your project:

```yaml
sources:
  - path: /data/weather/
    domain: earthscience
    description: "NOAA daily weather observations for Pacific Northwest"
    files:
      daily_obs.csv:
        description: "Daily temperature and precipitation readings"

  - path: az://myaccount/container/grid/
    domain: powergrid
    description: "Geospatial transmission line dataset"

search:
  embedding_model: nomic-ai/nomic-embed-text-v1.5  # or: azure-openai
```

### MCP Tools

The catalog exposes three tools to the agent:

| Tool | Description |
|------|-------------|
| `search_data` | Hybrid keyword + vector search with optional domain/source_type filters |
| `get_artifact` | Get full metadata for a specific artifact by ID |
| `list_domains` | List all available data domains |

### Deployment

| Concern | Local | Azure Container Apps |
|---------|-------|---------------------|
| File storage | Filesystem / mounted volume | Azure Blob Storage (managed identity) |
| Catalog DB | SQLite file in working dir | SQLite file in container |
| Embeddings | Local model (default) | Local model OR Azure OpenAI (config) |

## Agentic Workflows

This repository uses [GitHub Copilot agentic workflows](https://github.com/githubnext/agentics) for automated maintenance tasks. The workflows are authored as Markdown files in `.github/workflows/` and compiled to `.lock.yml` files via `gh aw compile` (lock files are auto-generated and should not be edited manually).

| Workflow | Trigger | Description |
|----------|---------|-------------|
| **Daily Repo Status** (`daily-repo-status.md`) | Runs on a daily schedule and on manual `workflow_dispatch` | Creates a GitHub issue summarizing recent repository activity — open issues, PRs, code changes, and actionable recommendations for maintainers. Issues are labeled `report` and `daily-status`. |
| **Copilot Issue Planner** (`copilot-issue-planner.md`) | Triggered when an issue is labeled `needs-spec`, or when a contributor comments `/plan` on an issue | Generates a structured implementation plan (summary, assumptions, checklist, risks, definition of done) as a comment on the issue. Useful for scoping work before starting development. |
| **Update Docs** (`update-docs.md`) | Runs on schedule and on manual `workflow_dispatch` | Scans for documentation gaps and opens issues describing areas that need updates. |
| **Agent Divergence Report** (`agent-divergence-report.md`) | Runs weekly on Tuesday and on manual `workflow_dispatch` | Compares agent implementations and opens an issue highlighting improvements. Issues are labeled `agent-divergence`. |

## Access

This repository is internal to Microsoft. For access and permissions:
- Read access requires membership in the appropriate Microsoft organization
- For role-based permissions (write, admin), contact the repository maintainers
- If the repository is set to internal visibility, ensure you have the 1ES-Enterprise-Visibility MyAccess group access

## Contributing

This repository welcomes contributions from Microsoft researchers and engineers.

**Contribution Guidelines:**
- Outside contributors should start with forks rather than branches
- For changes more complex than typos, please submit an issue first to discuss the proposed changes
- Follow the development practices outlined in the project documentation

For new collaborators, these [tips & tricks on InnerSource Communication](https://aka.ms/StartRight/README-Template/innerSource/2021_02_TipsAndTricksForCollaboration) may be helpful.

### Contact

For questions or feedback, contact: agora@microsoft.com


This project welcomes contributions and suggestions.  Most contributions require you to agree to a
Contributor License Agreement (CLA) declaring that you have the right to, and actually do, grant us
the rights to use your contribution. For details, visit [Contributor License Agreements](https://cla.opensource.microsoft.com).

When you submit a pull request, a CLA bot will automatically determine whether you need to provide
a CLA and decorate the PR appropriately (e.g., status check, comment). Simply follow the instructions
provided by the bot. You will only need to do this once across all repos using our CLA.

This project has adopted the [Microsoft Open Source Code of Conduct](https://opensource.microsoft.com/codeofconduct/).
For more information see the [Code of Conduct FAQ](https://opensource.microsoft.com/codeofconduct/faq/) or
contact [opencode@microsoft.com](mailto:opencode@microsoft.com) with any additional questions or comments.

## Trademarks

This project may contain trademarks or logos for projects, products, or services. Authorized use of Microsoft
trademarks or logos is subject to and must follow
[Microsoft's Trademark & Brand Guidelines](https://www.microsoft.com/legal/intellectualproperty/trademarks/usage/general).
Use of Microsoft trademarks or logos in modified versions of this project must not cause confusion or imply Microsoft sponsorship.
Any use of third-party trademarks or logos are subject to those third-party's policies.
