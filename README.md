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
├── agora/              # Agent implementations
│   ├── plan_then_execute/  # Plan-then-execute agent
│   └── toolmaker/          # Toolmaker agent
│   ├── agora/          # AgoraAgent (standalone, no BaseAgent inheritance)
│   ├── gui/            # GUIAgent for the interactive map GUI
│   ├── plan_then_execute/  # PlanThenExecuteAgent variant
│   └── toolmaker/      # ToolMakerAgent: four-phase agent for dynamic tool creation
├── auth/               # Agent-side credentials (ChainedTokenCredential)
├── middleware/         # Pluggable conversation middleware
├── tools/              # Tool search, MCP server registry, tool catalog
│   ├── search/         # search_tools backends (BM25, Azure AI Search)
│   └── toolmaker/      # create_tool_from_repo FunctionTool for AgoraAgent integration
├── domains/            # Domain-specific servers and tools
│   ├── example/        # Reference implementation
│   ├── powergrid/      # Power grid analysis (PyPSA, PyPower, HiGHS)
│   │   └── skills/     # Domain skills (grid-converter: PJM N-1 study → PyPSA network)
│   ├── process/        # Process simulation (IDAES, Pyomo)
│   ├── foundry/        # Azure AI Foundry integration
│   ├── dwsim/          # DWSIM chemical process simulation
│   ├── gis/            # Geospatial analysis (GeoPandas, Shapely, Rasterio, Folium)
│   ├── vitrimer_tg_sim/# Vitrimer Tg estimation via EMC + LAMMPS
│   ├── vitrimer_vae/   # Vitrimer inverse design via HierVAE + BO
│   └── office/         # Office document processing (Excel, Word, PowerPoint) with IRM support
├── code_execution/     # CodeExecutionServer base class, sessions, Docker config
│   └── deploy/         # Azure Container Apps deployment (Bicep + deploy script)
├── data_lake/          # Artifact registry, sync pipeline, Purview integration
│   └── utilities/      # Standalone helpers: update_purview_entity, list_artifact_registry
├── gui/                # GIS agent GUI (FastAPI backend + React/Vite frontend)
├── examples/           # Example agent run scripts
├── server_registry.yaml    # MCP server configurations
└── pyproject.toml
```

### Domain Skills

Some domains ship **skills** — curated knowledge packages that teach the agent how to perform complex, domain-specific workflows. Skills live under `domains/<domain>/skills/` and each contains a `SKILL.md` entry point plus supporting knowledge documents.

| Domain | Skill | Description |
|---|---|---|
| `powergrid` | [`grid-converter`](src/domains/powergrid/skills/grid-converter/SKILL.md) | Convert a PJM N-1 study Excel file (.xlsx) into a solvable PyPSA power grid model (.nc) and GeoJSON map. Covers bus/line parsing, impedance computation, generator matching (EIA/OSM), demand allocation (EIA-930), transformer detection, and network cleanup. |

### Domain Servers

| Domain | Server module | Notes |
|---|---|---|
| `powergrid` | `domains.powergrid.server.powergrid_server` | Grid analysis and optimization |
| `process` | `domains.process.server.process_server` | Process simulation tooling |
| `foundry` | `domains.foundry.server.foundry_server` | Azure AI Foundry integration |
| `dwsim` | `domains.dwsim.server.dwsim_server` | DWSIM-backed process workflows |
| `gis` | `domains.gis.server.gis_server` | Geospatial workflows used by GUI |
| `office` | `domains.office.server.office_server` | Office document processing |
| `openlca` | `domains.openlca.server.openlca_server` | OpenLCA workflows |
| `vitrimer_tg_sim` | `domains.vitrimer_tg_sim.server.vitrimer_tg_sim_server` | Tg estimation from MD simulation |
| `vitrimer_vae` | `domains.vitrimer_vae.server.vitrimer_vae_server` | AI-guided vitrimer inverse design |

See [`src/domains/vitrimer/README.md`](src/domains/vitrimer/README.md) for vitrimer-specific setup, dependencies, and tools.

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

### Running an Agent

```python
import asyncio
from agora import AgoraAgent
from dotenv import load_dotenv

load_dotenv()

agent = AgoraAgent(
    domain_prompt_path="domains/example/domain_prompt/example.jinja",
    llm="gpt-4o",
)

async def main():
    async with agent:
        result = await agent.go("Your query here")
        print(result.text)

asyncio.run(main())
```

MCP server tools (`execute_code`, session management) are auto-discovered from `server_registry.yaml` at startup — no manual tool registration is required. Start the MCP server first (see `code_execution/docker/`), then run one of the scripts in `examples/`.

To enable the [Toolmaker](#toolmaker) capability — which allows the agent to create new tools at runtime from a GitHub repository — pass `enable_toolmaker=True`:

```python
agent = AgoraAgent(
    domain_prompt_path="domains/example/domain_prompt/example.jinja",
    llm="gpt-4o",
    enable_toolmaker=True,
    toolmaker_llm="gpt-4o",  # optional; defaults to the main llm
)
```

## GUIAgent

`GUIAgent` powers the interactive map UI (`gui/`). In addition to core tool wiring, it supports caller-supplied context and middleware injection:

- `context_providers: Optional[list]`
- `middleware: Optional[list]`

These are appended to the built-in providers (history, compaction, experience, skills).

```python
from gui.agent import GUIAgent
from middleware import DecisionLogChatMiddleware, DecisionLogContextProvider
from middleware.decision_log import DecisionLog

decision_log = DecisionLog()
chat_middleware = DecisionLogChatMiddleware(
    decision_log=decision_log,
    agent_name="gui",
    chat_client=...,  # small synthesis model client
)

agent = GUIAgent(
    llm="gpt-4o",
    context_providers=[DecisionLogContextProvider(decision_log, chat_middleware=chat_middleware)],
    middleware=[chat_middleware],
)
```

See:

- [`src/gui/README.md`](src/gui/README.md) for GUIAgent architecture and tool behavior.
- [`src/gui/README.md`](src/gui/README.md) for frontend/backend endpoints.

## Development

```bash
# Run tests
uv run pytest                             # all configured test paths
uv run pytest src/code_execution/tests/   # code execution tests

# Lint and format
uv run pre-commit run --all-files
```

## Context Management

Conversation history is managed automatically by the MAF-native `CompactionProvider` registered inside each `BaseLLMExecutor`. The compaction pipeline applies three strategies in order:

1. **Tool-result compaction** — truncates verbose tool outputs, keeping only the most recent tool-call group verbatim.
2. **LLM summarization** — condenses older turns via an LLM call when the conversation exceeds a threshold length.
3. **Sliding window** — retains a fixed number of recent message groups as a hard cap.

No manual configuration is required — the pipeline is wired automatically when an executor initialises its MAF `Agent`.

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

## Toolmaker

The Toolmaker allows `AgoraAgent` to **dynamically create, build, test, and register new domain tools at runtime** from any public GitHub repository. When the agent encounters a task that no existing tool can handle, it invokes the Toolmaker as a sub-agent to synthesize and deploy a new MCP domain server.

### How it works

`ToolMakerAgent` (`toolmaker/`) runs a four-phase workflow:

| Phase | Name | Description |
|:---:|---|---|
| 1 | **Exploration** | Explores the GitHub repo and collaboratively builds a `TaskSpec` (tool name, arguments, return type, example invocations) |
| 2 | **Build & Test** | Generates domain server code, builds a Docker image, runs tests, and iterates until all tests pass |
| 3 | **User Decision** | Asks whether to keep the tool as _reusable_ (registered permanently) or _session-only_ (no registration) |
| 4 | **Registration** | (reusable path only) Registers the new domain in `server_registry.yaml` and related config files |

When `ToolMakerAgent` is invoked as a sub-agent of `AgoraAgent`, it still runs the full workflow above. In particular, Phase 3 lets the user choose whether the generated tool should be kept as _reusable_ or _session-only_; Phase 4 only runs if the reusable path is selected.

### Enabling Toolmaker in AgoraAgent

Pass `enable_toolmaker=True` to `AgoraAgent`. The agent gains a `create_tool_from_repo` tool and will automatically invoke it when it cannot satisfy a request with existing tools.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `enable_toolmaker` | `bool` | `False` | Adds the `create_tool_from_repo` tool to the agent |
| `toolmaker_llm` | `str` | same as `llm` | Model used by the ToolMaker sub-agent |

```python
agent = AgoraAgent(
    llm="gpt-4o",
    enable_toolmaker=True,
    toolmaker_llm="gpt-4o",  # optional: use a different model for ToolMaker
)
```

### Using ToolMakerAgent directly

You can also run `ToolMakerAgent` standalone to interactively create and register a new domain server:

```python
import asyncio
from toolmaker import ToolMakerAgent
from dotenv import load_dotenv

load_dotenv()

agent = ToolMakerAgent(llm="gpt-4o", max_iterations=500)

async def main():
    async with agent:
        result = await agent.go(
            "Create a tool from https://github.com/zopefoundation/roman "
            "that converts integers to Roman numeral strings."
        )
        print(result.text)

asyncio.run(main())
```

### Tool interface (`tools/toolmaker/toolmaker_tool.py`)

`create_toolmaker_function()` returns a MAF `FunctionTool` that wraps the ToolMaker sub-agent. Key parameters:

| Parameter | Default | Description |
|---|---|---|
| `llm` | `"gpt-5.1_2025-11-13"` | Model for the ToolMaker workflow |
| `max_iterations` | `500` | Maximum LLM iterations across all phases |
| `input_handler` | auto-resolve | `async (question, context) -> str` callback for mid-workflow prompts; defaults to a non-blocking auto-resolver |
| `base_tools` | `None` | Mutable list of the executor's tools; newly created MCP tools are appended here so they persist across agent turns |

### Examples

| Script | Description |
|---|---|
| [`examples/run_agent_with_toolmaker.py`](src/examples/run_agent_with_toolmaker.py) | AgoraAgent with `enable_toolmaker=True`; creates a Roman numeral tool on demand |
| [`examples/run_aurora_toolmaker.py`](src/examples/run_aurora_toolmaker.py) | Creates a weather prediction tool from Microsoft's Aurora foundation model |
| [`examples/run_toolmaker_humanize.py`](src/examples/run_toolmaker_humanize.py) | Standalone ToolMakerAgent wrapping the `humanize` library |
| [`examples/run_toolmaker_roman.py`](src/examples/run_toolmaker_roman.py) | Standalone ToolMakerAgent with blind-test validation |

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

### GUI Experience system and map capture

- Persistent per-user experience is stored in `gui/experiences/default.md`, auto-updated from conversations, and injected into every GUI session.
- GUI backend experience endpoints:
  - `GET /api/experience`
  - `PUT /api/experience`
  - `POST /api/experience/summarize`
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
| **Agent Divergence Report** (`agent-divergence-report.md`) | Runs weekly on Tuesday and on manual `workflow_dispatch` | Compares the agent implementations under `agora/`, `plan_then_execute/`, `toolmaker/` and opens an issue highlighting improvements that could propagate across agents. Issues are labeled `agent-divergence`. |

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
