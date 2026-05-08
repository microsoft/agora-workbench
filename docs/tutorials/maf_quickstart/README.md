# MAF + agora-workbench Quickstart

Wire a [Microsoft Agent Framework](https://github.com/microsoft/agent-framework)
agent to two agora-workbench tools and watch it answer a chemistry question
end-to-end.

## What you'll build

A single MAF agent with two tools:

```
                         ┌──────────────────────────┐
                         │  search_data_lake_catalog│  ← Azure AI Search
              ┌──────────┤  (data_lake adapter)     │     (DATA_LAKE_*)
              │          └──────────────────────────┘
   MAF Agent ─┤
              │          ┌──────────────────────────┐
              └──────────┤  chemistry MCP toolset   │  ← local Docker
                         │  (MCPStreamableHTTPTool) │     :8020/mcp
                         └──────────────────────────┘
```

The agent receives a prompt, calls `search_data_lake_catalog` to find a
chemistry dataset, then calls `execute_chemistry_code` (RDKit) to compute a
molecular descriptor, and reports the result.

## Prerequisites

- [`uv`](https://github.com/astral-sh/uv) installed.
- Docker (for the chemistry MCP server).
- `az login` completed (the default LLM and data lake paths use Entra ID).
- `.env` populated at the repo root. Copy entries from
  [.env.example](.env.example) and the repo-level
  [.env.example](../../../.env.example) as needed.

## Setup

### 1. Configure environment

Copy the tutorial's [.env.example](.env.example) into your repo-root `.env`
(or merge missing keys). The TRAPI Azure OpenAI endpoint is the default and
tested LLM path.

### 2. Build & start the chemistry MCP server

One-time base image build (the chemistry image inherits from it):

```bash
cd src
docker build -f deployment/mcp_server/base.Dockerfile -t mcp-server-base:local .
```

Then start the chemistry server (binds to `127.0.0.1:8020`):

```bash
cd src/domain_examples/chemistry
docker compose up -d --build
```

Confirm it's healthy:

```bash
curl http://localhost:8020/health
# => {"status":"healthy", ...}
```

The first start takes a few minutes because the conda environment is built
from scratch with RDKit and friends; subsequent starts are fast.

### 3. Run the agent

From the repo root:

```bash
uv run python docs/tutorials/maf_quickstart/agent.py
```

You should see log lines for each step (build chat client, build data lake
tool, build chemistry tool, run agent), then the agent's final answer with a
descriptor value (e.g. molecular weight ≈ 180.16 g/mol for aspirin).

## Walkthrough

The runnable script is [agent.py](agent.py); each integration point is its
own function so you can map README sections to code.

### Step A — Build the chat client (BYO LLM)

[`step_a_chat_client`](agent.py) calls `build_chat_client()` from
[llm.py](llm.py). agora-workbench is **BYO LLM**: any object that satisfies
MAF's `ChatClient` protocol works. The tutorial factory dispatches on
`$LLM_PROVIDER`:

| `LLM_PROVIDER` | Backing class | Auth | Required env |
| --- | --- | --- | --- |
| `azure_openai_entra` *(default)* | `agent_framework.azure.AzureOpenAIChatClient` | Entra (`get_token_provider`) | `AZURE_OPENAI_ENDPOINT`, `AOAI_SCOPE`, `API_VERSION`, `AZURE_OPENAI_DEPLOYMENT_NAME` (or `MODEL_DEPLOYMENT_NAME`) |
| `azure_openai_apikey` | same | API key | `+ AZURE_OPENAI_API_KEY` (no `AOAI_SCOPE`) |
| `openai` | `agent_framework.openai.OpenAIChatClient` | API key | `OPENAI_API_KEY`, `OPENAI_MODEL` |
| `ollama` | same with custom `base_url` | none | `OLLAMA_BASE_URL`, `OLLAMA_MODEL` |

The Entra path delegates to [`auth.providers.get_token_provider()`](../../../src/auth/providers.py),
which returns a callable backed by the same `AzureCliCredential → ManagedIdentityCredential`
chain used everywhere else in the repo. No new credentials are needed.

> **TRAPI vs. standard Azure OpenAI** — TRAPI uses the non-default scope
> `api://trapi/.default`. Standard Azure OpenAI deployments use
> `https://cognitiveservices.azure.com/.default`. Set `AOAI_SCOPE`
> accordingly.

### Step B — Data lake search tool

[`step_b_data_lake_tool`](agent.py) calls
[`create_data_lake_search_tool()`](../../../src/data_lake/tools/adapters/maf.py)
with no arguments, which gives you the default `DefaultDataLakeSearchBackend`
(Azure AI Search). The tool exposes `search_data_lake_catalog` to the agent
with parameters: `query`, `domains`, `tags`, `artifact_types`, `top`,
`select_fields`, `search_mode`, `order_by`.

The search backend is an `ABC` — to add hard constraints (e.g. "only return
energy-domain assets") subclass `DataLakeSearchBackend` and pass an instance
via the `backend=` kwarg. See the docstring in
[maf.py](../../../src/data_lake/tools/adapters/maf.py) for an example.

### Step C — Chemistry MCP tool

[`step_c_chemistry_tool`](agent.py) instantiates
`MCPStreamableHTTPTool(name="chemistry", url=..., approval_mode="never_require")`
directly. Behind the scenes the chemistry server (see
[chemistry_server.py](../../../src/domain_examples/chemistry/server/chemistry_server.py))
exposes the standard agora-workbench code-execution toolset:

- `execute_chemistry_code` — run Python with RDKit pre-imported
- `chemistry_list_sessions`, `chemistry_get_session_info`,
  `chemistry_close_session`, `chemistry_push_object` — session management

The local server uses `create_noop_auth_config()` (any bearer token is
accepted) so the tutorial passes a dummy `Authorization: Bearer dev-token`
header via a custom `httpx.AsyncClient`. **Do not deploy noop auth to a
publicly reachable server.**

The tool probes `/health` before connecting; if the server isn't running you
get a clean skip message instead of a confusing error inside the agent loop.

### Step D — Build the agent

[`step_d_build_agent`](agent.py) is a one-liner:

```python
agent = chat_client.create_agent(
    name="chem_quickstart_agent",
    instructions=...,
    tools=tools,
)
```

The system prompt tells the agent how to chain the two tools (search → code
execution → report).

### Step E — Run a single turn

[`step_e_run`](agent.py) sends one prompt and prints the response text. MAF
handles the tool-calling loop (LLM emits a tool call → MAF dispatches to the
right tool → result feeds back into the conversation → repeat until done).

## Expected output

A successful run looks roughly like:

```
INFO maf_quickstart: Step A: built chat client AzureOpenAIChatClient
INFO maf_quickstart: Step B: built data lake search tool
INFO maf_quickstart: Step C: built chemistry MCP tool @ http://localhost:8020/mcp
INFO maf_quickstart: Step D: built agent with 2 tool(s)

======================================================================
USER: Find a chemistry dataset in the data lake. Then compute the
molecular weight of one example molecule using RDKit and report the value.
======================================================================

AGENT:
I found <dataset name> in the data lake catalog. Using aspirin
(SMILES: CC(=O)OC1=CC=CC=C1C(=O)O) as the example molecule, I computed its
molecular weight with RDKit: ~180.16 g/mol.
```

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `ValueError: Environment variable 'AZURE_OPENAI_ENDPOINT' is required` | `.env` not loaded or missing the key. Check the repo-root `.env`. |
| `Step C: chemistry MCP server unreachable at http://localhost:8020/health` | Docker container not running. `docker compose up -d` in `src/domain_examples/chemistry/`. |
| `azure.identity` errors / 401s on data lake search | `az login` expired — re-authenticate. |
| TRAPI 403 / "scope not allowed" | `AOAI_SCOPE` doesn't match the endpoint. Use `api://trapi/.default` for TRAPI, `https://cognitiveservices.azure.com/.default` for standard AOAI. |

## Cleanup

```bash
cd src/domain_examples/chemistry && docker compose down
```

## Next steps

This quickstart deliberately keeps the surface small. Once you've got it
working, layer in:

- **Tool search** — discover domain tools the agent can call
  programmatically inside `execute_chemistry_code`. See
  [`tools/search/adapters/maf_core.py`](../../../src/tools/search/adapters/maf_core.py).
- **Planning tools** — give the agent a persistent step ledger. See
  [`planning/adapters/maf.py`](../../../src/planning/adapters/maf.py).
- **Decision-log middleware** — record every chat completion + tool call.
  See [`middleware/decision_log/adapters/maf_protocols.py`](../../../src/middleware/decision_log/adapters/maf_protocols.py).
- **Tool-learning middleware** — surface anti-pattern vignettes before
  inference. See [`middleware/tool_learning/adapters/maf_function.py`](../../../src/middleware/tool_learning/adapters/maf_function.py).
- **Local data lake** — when [PR #67](https://github.com/microsoft/agora-workbench/pull/67)
  lands, swap `DefaultDataLakeSearchBackend` for `LocalDataLakeSearchBackend`
  and run fully offline.
