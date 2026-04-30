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
- **Data Lake Integration** — semantic search over blob storage artifacts with RBAC-aware retrieval via Azure AI Search and Microsoft Purview

## Structure

```
src/
├── auth/               # Agent-side credentials (ChainedTokenCredential)
├── middleware/         # Pluggable conversation middleware
├── tools/              # Tool search, MCP server registry, tool catalog
│   └── search/         # search_tools backends (BM25, Azure AI Search)
├── code_execution/     # CodeExecutionServer base class, sessions, Docker config
│   └── deploy/         # Azure Container Apps deployment (Bicep + deploy script)
├── data_lake/          # Artifact registry, sync pipeline, Purview integration
│   └── utilities/      # Standalone helpers: update_purview_entity, list_artifact_registry
├── gui/                # GIS map GUI (FastAPI backend + React/Vite frontend)
├── server_registry.yaml    # MCP server configurations
└── pyproject.toml
```

## Getting Started

### Prerequisites

- **Python 3.11+**
- **uv** package manager for dependency management
- **Git** with submodule support

### Installation

```bash
uv sync            # install dependencies
uv sync --group dev  # include dev tools (pytest, pre-commit, jupyter)
```

Copy `.env.example` to `.env` and configure credentials. See the comments in that file for available settings.

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

MCP servers can be deployed as Azure Container Apps. The deployment infrastructure lives in `code_execution/deploy/`:

- **`deploy.sh`** — builds a Docker image for a given server, pushes it to Azure Container Registry, and deploys or updates a Container App via Bicep.
- **`main.bicep`** — ARM template defining the Container App with health probes and HTTP-based auto-scaling.
- **`parameters/`** — per-server Bicep parameter files (e.g., `office.bicepparam`).

Quick start:

```bash
cd src/code_execution/deploy
./deploy.sh --server office   # build, push, and deploy the Office MCP server
```

See [`src/code_execution/deploy/README.md`](src/code_execution/deploy/README.md) for infrastructure setup and environment variables (`ACA_*`).

## Code Execution

### Output Truncation

`CodeExecutionServer` truncates large `stdout`/`stderr` to prevent bloating the LLM context window. Truncation is enabled by default and configurable.

| Setting | Default | Description |
|---|---|---|
| Constructor parameter `output_truncation_threshold` | `50_000` | Maximum characters allowed per output stream before truncation is applied (that is, `stdout` and `stderr` are evaluated independently, not as a combined total). A guidance message is appended instructing the LLM to inspect large objects server-side. |
| Environment variable `CODE_OUTPUT_TRUNCATION_THRESHOLD` | _(unset)_ | Overrides the constructor parameter when set. Takes precedence. |

Set to `0` to **disable** truncation entirely (not recommended for production — very large outputs will be passed to the LLM unchanged).

```python
# Custom threshold via constructor
server = MyCodeExecutionServer(
    environment_config=...,
    output_truncation_threshold=100_000,  # 100 k chars
)
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

## Data Lake

### Utility Functions (`data_lake/utilities/utilities.py`)

Standalone helpers for common Purview and Azure AI Search operations.

#### `update_purview_entity()`

Edits the display name and/or user description of a Purview entity identified by its blob URL (qualified name).

```python
from data_lake.utilities.utilities import update_purview_entity

# Rename a blob entity
update_purview_entity(
    purview_account="agora-purview",
    qualified_name="https://myaccount.blob.core.windows.net/container/path/file.csv",
    new_name="My Dataset",
    new_description="Monthly energy consumption figures.",
)

# Preview without making changes (dry run)
update_purview_entity(
    purview_account="agora-purview",
    qualified_name="https://myaccount.blob.core.windows.net/container/path/",
    new_description="Processed grid topology files.",
    dry_run=True,
)
```

**Parameters:**

| Parameter | Type | Description |
|---|---|---|
| `purview_account` | `str` | Purview account name (e.g. `"agora-purview"`) |
| `qualified_name` | `str` | Full blob URL of the entity |
| `new_name` | `str \| None` | New display name; `None` leaves it unchanged |
| `new_description` | `str \| None` | New user description; `None` leaves it unchanged |
| `dry_run` | `bool` | If `True`, log what would change without writing to Purview |

Directory paths (ending with `/`) are tried as `azure_blob_container` first, then `azure_blob_path`. At least one of `new_name` or `new_description` must be provided.

#### `list_artifact_registry()`

Queries the `artifact-registry` Azure AI Search index and returns all matching documents. Supports optional OData filter expressions.

```python
from data_lake.utilities.utilities import list_artifact_registry

# List all artifacts
artifacts = list_artifact_registry(search_service="agora-search")

# Filter by domain and type
artifacts = list_artifact_registry(
    search_service="agora-search",
    filter_expression="domain eq 'energy' and artifact_type eq 'blob'",
    top=100,
    select_fields=["id", "name", "description"],
)
```

**Parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `search_service` | `str` | — | Azure AI Search service name |
| `index_name` | `str` | `"artifact-registry"` | Target index name |
| `filter_expression` | `str \| None` | `None` | OData `$filter` expression |
| `top` | `int \| None` | `None` | Maximum results; `None` fetches all |
| `select_fields` | `list[str] \| None` | `None` | Fields to return; `None` returns all |

### Purview Sync Cleanup (`data_lake/sync/`)

The **Purview Sync Cleanup** GitHub Actions workflow (`.github/workflows/purview-sync-cleanup.yaml`) runs weekly (every Sunday at 02:00 UTC) and on demand. It removes stale artifact-registry entries whose Purview entity or blob no longer exists.

**What the workflow produces:**

| Output | Description |
|---|---|
| `cleanup-report.log` | Concise report surfaced in the GitHub Actions job summary. Includes run parameters, sync summary (processed/enriched/cleaned/failed counts), list of stale entries found, and any errors. |
| `cleanup-output.log` | Full verbose sync output uploaded as a workflow artifact. |
| Workflow artifacts | Both logs are uploaded with **180-day retention** under `cleanup-logs-<run_id>`. |

**OpenAI endpoint validation:** The workflow fails fast if `DATA_LAKE_VECTORIZER_ENDPOINT` (mapped from `vars.DATA_LAKE_VECTORIZER_ENDPOINT`) is not set, preventing a silent run with missing embeddings configuration.

**Trigger and configuration:**

```yaml
# Manual trigger with overrides
workflow_dispatch:
  inputs:
    dry_run:            # "true" to preview without deleting
    max_cleanup:        # max stale entries to delete (default: 50)
    cleanup_threshold:  # circuit-breaker ratio (default: 0.2)
    search_service:     # optional Azure AI Search service override
    purview_account:    # optional Microsoft Purview account override
```

Repository variables (`DATA_LAKE_*`) provide defaults for `search_service` (`DATA_LAKE_SEARCH_NAME`), `blob_details_index` (`DATA_LAKE_BLOB_DETAILS_INDEX`), and `artifact_registry_index` (`DATA_LAKE_CATALOG_INDEX_NAME`).

For cleanup safeguards (max cap, circuit breaker, transient error handling) see [`src/data_lake/sync/README.md`](src/data_lake/sync/README.md).

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
