# Code Execution Module

A framework for creating MCP servers that execute Python code in isolated environments with domain-specific package dependencies and stateful session management.

## Overview

Each domain (powergrid, process, foundry, etc.) defines a server under `domains/<name>/server/` that uses this module's `CodeExecutionServer` base class. The server provisions a Python environment (via uv, conda, or pip), exposes an `execute_<name>_code` MCP tool, and manages sessions with persistent state across calls.

The main components are:

- **`CodeExecutionServer`** (`code_execution/server.py`) — FastMCP-based server with subprocess code execution, Entra ID authentication, and customizable validation/preprocessing hooks
- **`EnvironmentConfig`** (`code_execution/code_execution_models.py`) — defines the Python environment type (`uv`, `conda`, `pip`), dependencies, and build settings
- **Environment Builders** (`code_execution/environment_builders.py`) — automated virtual environment creation
- **Session Management** (`code_execution/sessions/`) — stateful workflows across MCP calls, with decorators (`@auto_session_tool`, `@create_session_tool`, `@requires_session`), context-based session injection, and automatic cleanup
- **Tool-Learning Middleware** (`code_execution/tool_learning_middleware.py`) — MCP-side middleware that observes domain-tool failures and provides anti-pattern guidance to agents (see [Tool Learning](#tool-learning) below)

## Creating a Server

Domain servers live in `domains/<name>/server/` and are registered in `server_registry.yaml`. A minimal server:

```python
from code_execution import CodeExecutionServer, EnvironmentConfig

config = EnvironmentConfig(
    name="myenv",
    description="Execute Python code with custom packages",
    type="uv",
    dependency_file="numpy>=1.24.0\npandas>=2.0.0\n",
    auto_build=True,
)

server = CodeExecutionServer(environment_config=config)
await server.run_http(host="0.0.0.0", port=8000)
```

See `domains/example/server/example_server.py` for a complete reference implementation.

## Execution Modes and Session Meta Tools

### Background execution (`execute_<server>_code(background=True)`)

`execute_<server>_code` supports a `background` flag:

- `background=False` (default): run inline and return full execution output.
- `background=True`: submit execution to the current session kernel and return immediately with a job handle.

Example submission result:

```json
{
  "job_id": "j_1234abcd5678",
  "status": "running",
  "session_id": "..."
}
```

Use the `check_job` tool to poll status for jobs started with `background=True`.

### `check_job` tool

`check_job(job_id)` returns current background-job state and output:

- Running job: `job_id`, `session_id`, `status`, `elapsed_seconds`, partial `stdout`/`stderr`
- Terminal job (`completed` / `failed`): same fields plus `success` and optional `error`

Example terminal result:

```json
{
  "job_id": "j_1234abcd5678",
  "session_id": "...",
  "status": "completed",
  "elapsed_seconds": 12.341,
  "stdout": "...",
  "stderr": "",
  "success": true
}
```

Access control: background jobs are user-owned. `check_job` intentionally returns the same `"Job <id> not found"` error for both unknown IDs and unauthorized IDs.

### Background job lifecycle and concurrency

1. Submit with `execute_<server>_code(background=True)`.
2. Poll with `check_job(job_id)` until `status` is terminal.
3. When terminal, consume final output (`stdout`/`stderr`/`success`/`error`).

Only **one concurrent background job per session** is allowed. If a second background (or foreground) execution is attempted while one is running, the server returns a session-busy error with the active `job_id`.

Completed/failed background jobs are retained in-memory for bounded polling history and are eventually purged.

### Session inspection (`<server>_inspect_session`)

Each server registers a prefixed inspect meta tool (for example `gis_inspect_session`) that returns namespace and job state:

```json
{
  "success": true,
  "session_id": "...",
  "status": "idle",
  "job_id": null,
  "job_status": null,
  "namespace": {
    "df": { "type": "DataFrame", "repr": "..." }
  }
}
```

## Parallel execution tools

Each server also registers map-style parallel tools:

- `<server>_parallel_execute(code, inputs, timeout=3600, result_variable="result")`
- `<server>_check_batch(batch_id)`
- `<server>_cancel_batch(batch_id)`

### `<server>_parallel_execute`

Runs one code template across a list of input dictionaries. A dedicated child session/kernel is created per input.

Returns:

```json
{
  "batch_id": "b_1234abcd5678",
  "jobs": [
    { "job_id": "j_...", "session_id": "...", "status": "running", "input_index": 0 }
  ]
}
```

### `<server>_check_batch`

Returns aggregate batch status and per-job results:

```json
{
  "batch_id": "b_1234abcd5678",
  "parent_session_id": "s_abcdef123456",
  "status": "running",
  "completed": 1,
  "running": 2,
  "failed": 0,
  "jobs": [
    {
      "job_id": "j_...",
      "session_id": "...",
      "status": "completed",
      "input_index": 0,
      "result_variable": "result",
      "result": { "...": "..." },
      "execution": { "success": true, "stdout": "...", "stderr": "" }
    }
  ]
}
```

Batch lifecycle:

1. Submit batch with `<server>_parallel_execute`.
2. Poll with `<server>_check_batch`.
3. Optionally stop with `<server>_cancel_batch`.
4. Once a batch reaches terminal state and final status is read, child sessions are cleaned up and in-memory batch/job state is pruned.

Ownership rules: batch operations are tied to the submitting session/user context; callers must own the batch/session context to inspect or cancel it. Batch status payloads include `parent_session_id` so the server can validate that subsequent `<server>_check_batch` and `<server>_cancel_batch` calls are being made from the submitting session context.

### `<server>_cancel_batch`

Interrupts active child kernels, cancels outstanding tasks, performs cleanup, and returns a final aggregate payload in the same shape as `<server>_check_batch`.

### `PARALLEL_EXECUTE_MAX_CONCURRENCY`

Server-wide limit for concurrent parallel jobs:

- `0` (default): no semaphore cap (all jobs can start)
- `N > 0`: at most `N` parallel jobs execute at once; remaining jobs wait for a slot

Set via environment variable:

```bash
export PARALLEL_EXECUTE_MAX_CONCURRENCY=8
```

## Session-capacity behavior (rejection, no eviction)

When the configured `max_sessions` limit is reached, new session creation is **rejected**. The server no longer evicts least-recently-used sessions.

Clients receive a typed 429 payload:

```json
{
  "success": false,
  "error_type": "max_sessions_reached",
  "error": "Maximum number of sessions (...) reached. Please close an existing session before creating a new one.",
  "status_code": 429
}
```

Recommended handling: close an unused session (`<server>_close_session`) or retry with an existing active `session_id`.

## Tool Learning

The `VignetteMiddleware` is a FastMCP middleware that helps agents avoid repeating domain-tool mistakes. It operates automatically on every `execute_*_code` call:

1. **Observe** — After code execution, the middleware parses `ToolCallRecord`s from the result. For each failed domain-tool call, it compiles an anti-pattern vignette describing the error and the arguments that triggered it.
2. **Persist** — Vignettes are upserted to Azure Table Storage, with confidence scores that increase on repeated observations.
3. **Retrieve & Advise** — On subsequent calls, the middleware fetches relevant vignettes from Azure AI Search for the domain tools that were actually invoked, and appends them to the response as a `domain_tool_guardrails` field. The agent sees this guidance before writing its next code block.

The middleware is registered automatically when `TOOL_LEARNING_TABLE_ENDPOINT` or `TOOL_LEARNING_SEARCH_ENDPOINT` is configured. It is a no-op when neither is set. Each backend is independent:

- **Both set** — full loop: observe failures → persist vignettes → retrieve and advise on subsequent calls.
- **Table only** — write-only: failures are recorded but never surfaced to agents. Useful for data collection before enabling retrieval.
- **Search only** — read-only: vignettes are retrieved and returned to agents, but new failures are not persisted. Useful when vignettes are populated externally or via a separate pipeline.

For the client-side MAF adapters (`middleware.tool_learning.adapters.VignetteFunctionMiddleware`), you can also set `TOOL_LEARNING_LOCAL_DIR` to persist write-path vignettes locally (without Azure Table Storage).

Authentication uses the server credential (`AzureCliCredential` in simulation mode, `ManagedIdentityCredential` in production) — see `auth/server_credential.py`.

### Azure Resource Setup

Tool learning requires two Azure resources:

**1. Azure Table Storage** — stores vignette entities (source of truth).

```bash
# Create a storage account (or use an existing one)
az storage account create \
  --name <storage-account> \
  --resource-group <rg> \
  --kind StorageV2

# Create the table (default name: ToolVignettes)
az storage table create \
  --name ToolVignettes \
  --account-name <storage-account>

# Grant the server's managed identity access
az role assignment create \
  --role "Storage Table Data Contributor" \
  --assignee <managed-identity-client-id> \
  --scope /subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.Storage/storageAccounts/<storage-account>
```

**2. Azure AI Search** — provides hybrid (keyword + vector) retrieval of vignettes.

```bash
# Create a search service (or use an existing one)
az search service create \
  --name <search-service> \
  --resource-group <rg> \
  --sku basic

# Grant the server's managed identity access
az role assignment create \
  --role "Search Index Data Contributor" \
  --assignee <managed-identity-client-id> \
  --scope /subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.Search/searchServices/<search-service>
```

The search index (`tool-vignettes` by default) is populated by an Azure AI Search Table indexer with an embedding skillset. Use the deployment script in `vignette_deployment/` to create all search resources:

```bash
# Deploy the index (once)
cd vignette_deployment
uv run deploy.py \
  --search-endpoint <search-endpoint-url> \
  --azure-openai-endpoint <openai-endpoint> \
  index

# Deploy a data source + skillset + indexer
uv run deploy.py \
  --search-endpoint <search-endpoint-url> \
  --azure-openai-endpoint <openai-endpoint> \
  source \
  --source-id <unique-id> \
  --storage-resource-id <storage-account-resource-id> \
  --managed-identity-id <managed-identity-resource-id>
```

See `deployment/tool_learning_middleware/README.md` for full details on the index schema and deployment options.

### Environment Variables

| Variable | Description | Default |
|---|---|---|
| `TOOL_LEARNING_TABLE_ENDPOINT` | Table Storage endpoint (e.g. `https://<account>.table.core.windows.net`) | _(disabled)_ |
| `TOOL_LEARNING_TABLE_NAME` | Table name for vignette entities | `ToolVignettes` |
| `TOOL_LEARNING_LOCAL_DIR` | Local vignette directory for client-side MAF write backend when table endpoint is unset | _(disabled)_ |
| `TOOL_LEARNING_SEARCH_ENDPOINT` | AI Search endpoint (e.g. `https://<service>.search.windows.net`) | _(disabled)_ |
| `TOOL_LEARNING_SEARCH_INDEX` | AI Search index name | `tool-vignettes` |
| `TOOL_LEARNING_TOP_K` | Max vignettes retrieved per tool | `5` |
| `TOOL_LEARNING_MIN_CONFIDENCE` | Minimum confidence threshold | `0.0` |

## Docker

Server images are built from a single multi-stage `Dockerfile` in `docker/`. Building requires Azure CLI credentials (run `az login` first) — see `docker/README.md` for full build and run instructions.

## Object Transfer

MCP servers can transfer Python objects directly to each other without routing data through the agent context. This is useful for passing large datasets (e.g. a GeoDataFrame computed in one server) to a second server for further processing.

### How it works

1. The agent calls `{server}_push_object` on the source server, specifying the target server URL, source variable name, and optional target variable name.
2. The source server serializes the named variable from the kernel namespace using `dill` and POSTs it to the target server's `/object-transfer/receive` endpoint.
3. The target server deserializes the payload and injects the object into its kernel namespace.
4. The agent can then reference the variable by name on the target server in subsequent `execute_code` calls.

### Constraints

| Constraint | Value |
|------------|-------|
| Maximum object size | 256 MB (serialized) |
| Serialization format | `dill` (supports lambdas, closures, and nested classes that standard `pickle` cannot handle) |
| Authentication | Bearer token from the calling session is forwarded to the target server; the `/object-transfer/receive` endpoint requires the same Entra ID auth as `/mcp` |

### Example (agent-side)

```python
# Push a variable named "gdf" from the GIS server to the process server
await gis_push_object(
    target_url="http://process-server:8002",
    variable_name="gdf",
    target_variable_name="imported_gdf",  # name on the target server (optional)
)
```

The target server then makes `imported_gdf` available in all subsequent code execution calls for that session.

## Tests

```bash
cd code_execution
uv run pytest tests/
```
