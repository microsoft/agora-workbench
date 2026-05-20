# MAF + agora-workbench Quickstart

Wire a [Microsoft Agent Framework](https://github.com/microsoft/agent-framework)
agent to two agora-workbench tools and watch it answer a chemistry question
end-to-end.

## What you'll build

A single MAF agent with two tool sources:

```
                         ┌────────────────────────┐
                         │  search_data_lake_catalog│  ← Azure AI Search
              ┌──────────┤  (data_lake adapter)     │     (DATA_LAKE_*)
              │          └────────────────────────┘
   MAF Agent ─┤
              │          ┌────────────────────────┐
              └──────────┤  chemistry MCP toolset   │  ← local Docker
                         │  (MCPStreamableHTTPTool) │     :8020/mcp
                         └────────────────────────┘
                              │
                              ├─ typed tools (parse_molecule,
                              │    compute_descriptors,
                              │    filter_drug_candidates,
                              │    compute_fingerprints,
                              │    find_similar_molecules,
                              │    cluster_molecules, …)
                              └─ execute_chemistry_code (escape hatch)
```

The agent receives a prompt, calls `search_data_lake_catalog` to find a
chemistry dataset, then chains the typed chemistry tools (parse →
compute_descriptors → filter_drug_candidates) to screen a small molecule
library for drug-likeness, and reports the results. The chemistry domain's
[`SKILL.md`](../../../src/domain_examples/chemistry/skills/SKILL.md) is
injected into the system prompt so the agent follows the recommended
state-graph workflow.

## Prerequisites

**Required:**

- [`uv`](https://github.com/astral-sh/uv) installed.
- An LLM you can call. The default path is **Azure OpenAI via Entra ID**
  (run `az login` first), but the BYO-LLM factory in [chat_client.py](chat_client.py) also
  supports Azure OpenAI API keys, OpenAI, and Ollama — see Step A.
- `.env` populated at the repo root. Copy entries from
  [.env.example](.env.example) and the repo-level
  [.env.example](../../../.env.example) as needed.

**Optional (the script degrades gracefully if these are missing):**

- **Docker** — needed only for the chemistry MCP server (Step C). Without
  it, the agent runs with just the data lake tool.
- **Data lake catalog (Step B)** — pick one of:
  - **Azure AI Search-backed** — set `DATA_LAKE_SEARCH_ENDPOINT` (and
    `DATA_LAKE_CATALOG_INDEX_NAME`). Best for shared/production catalogs.
  - **Local YAML catalog** — set `DATA_LAKE_LOCAL_CATALOG` to a YAML
    path (e.g. `src/data_lake/tools/adapters/catalog.example.yaml`).
    Best for solo dev without Azure; BM25 keyword search runs locally.
    See [Run with a local-only data lake](#run-with-a-local-only-data-lake)
    below.
  - Leave both unset to skip Step B entirely; the script logs a skip
    message and runs with chemistry tools only.
You need **at least one of** the data lake or the chemistry MCP server
configured for the agent to have any tools to call.

## Setup

### 1. Configure environment

Copy the tutorial's [.env.example](.env.example) into your repo-root `.env`
(or merge missing keys). The default and tested LLM path is **Azure OpenAI
via Entra ID**.

### 2. Build & start the chemistry MCP server

> **⚠️ Local-dev auth only** — the bundled chemistry MCP server uses
> [`create_noop_auth_config()`](../../../src/code_execution/code_execution/auth/),
> which accepts any bearer token (the tutorial sends a dummy
> `Authorization: Bearer dev-token`). It binds to `127.0.0.1:8020` so it's
> not reachable from outside the host. **Do not deploy this configuration
> to a publicly reachable server.** For production, swap in a real
> auth config (see [Step C](#step-c--chemistry-mcp-tool) and the
> [chemistry server source](../../../src/domain_examples/chemistry/server/chemistry_server.py)).

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
[chat_client.py](chat_client.py). agora-workbench is **BYO LLM**: any object that satisfies
MAF's `ChatClient` protocol works. The tutorial factory is a thin wrapper around
the framework-agnostic [`ModelSpec`](../../../src/llm/spec.py) +
[`make_maf_client`](../../../src/llm/factories/maf.py) abstraction in `src/llm/`,
and dispatches on `$LLM_PROVIDER`:

| `LLM_PROVIDER` | Backing class | Auth | Required env |
| --- | --- | --- | --- |
| `azure_openai_entra` *(default)* | `agent_framework.openai.OpenAIChatClient` (Azure mode via `azure_endpoint=`) | Entra (`get_token_provider`) | `AZURE_OPENAI_ENDPOINT`, `AOAI_SCOPE`, `API_VERSION`, `AZURE_OPENAI_DEPLOYMENT_NAME` (or `MODEL_DEPLOYMENT_NAME`) |
| `azure_openai_apikey` | same | API key | `+ AZURE_OPENAI_API_KEY` (no `AOAI_SCOPE`) |
| `openai` | `agent_framework.openai.OpenAIChatClient` | API key | `OPENAI_API_KEY`, `OPENAI_MODEL` |
| `ollama` | same with custom `base_url` | none | `OLLAMA_BASE_URL`, `OLLAMA_MODEL` |

> **`agent-framework` 1.2 note** — earlier versions exposed a separate
> `agent_framework.azure.AzureOpenAIChatClient`. As of `agent-framework`
> 1.2, that class was removed and the unified
> `agent_framework.openai.OpenAIChatClient` accepts an `azure_endpoint=`
> kwarg to switch into Azure mode. The factory uses the new API.

The Entra path delegates to [`auth.providers.get_token_provider()`](../../../src/auth/providers.py),
which returns a callable backed by the same `AzureCliCredential → ManagedIdentityCredential`
chain used everywhere else in the repo. No new credentials are needed.

> **AOAI scope** — standard Azure OpenAI deployments use the scope
> `https://cognitiveservices.azure.com/.default`. Some internal/gateway
> endpoints require a different scope; set `AOAI_SCOPE` to whatever your
> endpoint owner specifies.

> **API version on the Responses API** — `OpenAIChatClient` calls the
> `/responses` endpoint. Public Azure OpenAI accepts the usual dated
> previews (e.g. `2025-04-01-preview`). Some internal gateways only
> accept the floating tags `preview` or `v1` on `/responses` and return
> `BadRequest: API version not supported` for dated strings — if you see
> that, set `API_VERSION="preview"`.

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
directly. The chemistry server (see
[chemistry_server.py](../../../src/domain_examples/chemistry/server/chemistry_server.py))
exposes a small set of MCP tools:

- `execute_chemistry_code` — run Python in a long-lived Jupyter kernel with
  RDKit pre-imported (`Chem`, `Descriptors`, `AllChem`, `rdMolDescriptors`,
  `np`, `pd`).
- `search_chemistry_tools` — **server-side** BM25 search over the domain's
  typed helper catalog. The server builds the index from its own
  `ToolRegistry` at startup, so no client-side indexing infrastructure is
  needed. Call with `(query: str, top: int = 5)`; pass `query=""` with
  `top=999` to enumerate the full catalog. Each result includes `name`,
  `description`, `execution_type`, `score`, `state_requires`, and
  `state_produces`.
- `check_job`, `chemistry_*` (sessions, parallel execute, push object) —
  session/lifecycle helpers.

**Typed domain helpers** are *not* separate MCP tools. They live in the
[`chemistry_tools`](../../../src/domain_examples/chemistry/chemistry_tools/)
pip package, which is installed into the kernel's conda env at server-build
time. The server then auto-injects an instrumented Python proxy for each
helper into the kernel namespace via
[`tool_proxy.py`](../../../src/code_execution/code_execution/tool_proxy.py),
so inside `execute_chemistry_code` the agent can simply call them as plain
Python functions — no imports required:

```python
# Example body the agent might send to execute_chemistry_code
result = filter_drug_candidates(
    ["CC(=O)OC1=CC=CC=C1C(=O)O", "CN1C=NC2=C1C(=O)N(C(=O)N2C)C"],
    rules="lipinski",
)
print(result)
```

| Helper | Purpose |
| --- | --- |
| `parse_molecule` | Canonical SMILES, formula, MW, atom/bond counts |
| `enumerate_functional_groups` | SMARTS-based functional group detection |
| `compute_descriptors` | MW, LogP, HBD/HBA, TPSA, rotatable bonds, Lipinski pass/fail |
| `filter_drug_candidates` | Screen a SMILES list against Lipinski / Veber rules |
| `compute_fingerprints` | Morgan / RDKit / MACCS fingerprints |
| `find_similar_molecules` | Tanimoto similarity search vs. a query |
| `cluster_molecules` | Butina clustering by fingerprint similarity |

Full definitions live in
[tools/definitions.py](../../../src/domain_examples/chemistry/tools/definitions.py).
A `list_tools()` function is also injected into the kernel namespace if the
agent needs to enumerate them at runtime.

The local server uses `create_noop_auth_config()` (any bearer token is
accepted) so the tutorial passes a dummy `Authorization: Bearer dev-token`
header via a custom `httpx.AsyncClient`. **Do not deploy noop auth to a
publicly reachable server.**

The tool probes `/health` before connecting; if the server isn't running you
get a clean skip message instead of a confusing error inside the agent loop.

### Step D — Build the agent

[`step_d_build_agent`](agent.py) reads the chemistry domain's
[`SKILL.md`](../../../src/domain_examples/chemistry/skills/SKILL.md) (a
portable workflow guide that documents the tool state-graph, default
parameters, and common pitfalls) and appends it to the system prompt. This
is the simplest version of the agora-workbench *skills* pattern — domain
knowledge travels with the domain instead of being hard-coded into the
agent. The MAF agent is then built with a single call:

```python
agent = chat_client.as_agent(
    name="chem_quickstart_agent",
    instructions=...,  # base instructions + injected SKILL.md
    tools=tools,
)
```

### Step E — Run a single turn

[`step_e_run`](agent.py) sends one prompt and prints the response text. MAF
handles the tool-calling loop (LLM emits a tool call → MAF dispatches to the
right tool → result feeds back into the conversation → repeat until done).

## Expected output

A successful run looks roughly like:

```
INFO maf_quickstart: Step A: built chat client OpenAIChatClient
INFO maf_quickstart: Step B: built data lake search tool
INFO maf_quickstart: Step C: built chemistry MCP tool @ http://localhost:8020/mcp
INFO maf_quickstart: Step D: built agent with 2 tool(s); skills injected: chemistry=True, energy=True

======================================================================
USER: Look for chemistry datasets in the data lake … then screen this
small library of molecules for drug-likeness …
======================================================================

AGENT:
I searched the data lake catalog …
Drug-likeness screening (Lipinski's Rule of Five):
  - aspirin       → PASS  (MW ≈ 180.16, LogP ≈ 1.19)
  - caffeine      → PASS  (MW ≈ 194.19, LogP ≈ -1.03)
  - ibuprofen     → PASS  (MW ≈ 206.28, LogP ≈ 3.07)
  - atorvastatin  → FAIL  (MW ≈ 558.65 > 500)
```

(Exact values may differ slightly run-to-run depending on which RDKit
version the chemistry environment resolves.)

## Run with a local-only data lake

If you don't have access to Azure AI Search, you can still exercise Step B
against a YAML catalog on disk. The repo ships an example at
[`src/data_lake/tools/adapters/catalog.example.yaml`](../../../src/data_lake/tools/adapters/catalog.example.yaml).

In your `.env`, comment out the Azure search keys and set the local path:

```dotenv
# DATA_LAKE_SEARCH_ENDPOINT=...        # leave unset/commented
# DATA_LAKE_CATALOG_INDEX_NAME=...     # leave unset/commented
DATA_LAKE_LOCAL_CATALOG="src/data_lake/tools/adapters/catalog.example.yaml"
```

> **Note** — `agent.py` calls `load_dotenv()` with the default
> `override=False`. If `DATA_LAKE_SEARCH_ENDPOINT` is already set in your
> shell or `.env`, an empty value in the shell won't override it; either
> remove the key from `.env` or set it to an empty string in `.env`
> itself.

Then run the agent as usual:

```bash
uv run python docs/tutorials/maf_quickstart/agent.py
```

You should see a startup line like:

```
Discovered 3 domain(s) in local catalog: ['earthscience', 'energy', 'powergrid']
Step B: built data lake search tool
```

The agent's `search_data_lake_catalog` tool now returns hits ranked by
BM25 over the YAML's `name`/`description`/`tags` fields. Schema details
and how to point `storage_url` at real files (local FS, mounted volume,
Azurite) are in [`src/data_lake/README.md`](../../../src/data_lake/README.md#local-development-no-azure-credentials).

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `ValueError: Environment variable 'AZURE_OPENAI_ENDPOINT' is required` | `.env` not loaded or missing the key. Check the repo-root `.env`. |
| `ImportError: cannot import name 'AzureOpenAIChatClient' from 'agent_framework.azure'` | You're on `agent-framework >= 1.2`, which removed that class. The tutorial's [chat_client.py](chat_client.py) already targets the unified `agent_framework.openai.OpenAIChatClient`; if you've forked or pinned to an older version, either update or pin `agent-framework<1.2`. |
| `BadRequest: API version not supported` from `/responses` | The Responses API on your endpoint doesn't accept the configured `API_VERSION`. Try `API_VERSION="preview"` (some internal gateways only accept floating tags; public AOAI typically wants a dated preview like `2025-04-01-preview`). |
| `404 DeploymentId Not Found` | The deployment id doesn't exist on your endpoint. Internal gateways often require dated ids like `gpt-5.2-codex_2026-01-14`. |
| `Bind for 127.0.0.1:8020 failed: port is already allocated` | A previous chemistry container (or unrelated process) is still holding the port. Find it with `docker ps \| grep 8020` and remove with `docker rm -f <name>`, then retry `docker compose up -d`. |
| Container exits immediately with `Could not resolve host: conda.anaconda.org` | Transient DNS / network blip while the conda env is being built on first start. Retry: `docker compose down && docker compose up -d`. |
| `Step C: chemistry MCP server unreachable at http://localhost:8020/health` | Docker container not running. `docker compose up -d` in `src/domain_examples/chemistry/`. |
| `azure.identity` errors / 401s on data lake search | `az login` expired — re-authenticate. |
| AOAI 403 / "scope not allowed" | `AOAI_SCOPE` doesn't match the endpoint. Standard AOAI uses `https://cognitiveservices.azure.com/.default`; some internal/gateway endpoints require a different scope — check with your endpoint owner. |
| Container exits during startup with `RuntimeError: Additional command 1/1 failed` | The `chemistry_tools` pip-install step inside the conda env failed; the build now surfaces this instead of silently continuing. Read the surrounding container logs for the underlying pip error (network, missing build dep, etc.) and rebuild with `docker compose up --build`. |

## Cleanup

```bash
cd src/domain_examples/chemistry && docker compose down
```

## Next steps

This quickstart deliberately keeps the surface small. Once you've got it
working, layer in:

- **Server-side tool search** — the chemistry and energy systems servers
  already expose `search_chemistry_tools` and `search_energysystems_tools`
  as MCP tools, so the agent can discover domain helpers at runtime without
  any client wiring. The BM25 index is built from each server's
  `ToolRegistry` at startup; see
  [`_setup_search_tool`](../../../src/code_execution/code_execution/server.py)
  in `code_execution/server.py`.
- **Workflow planning** — servers with state-annotated tools also expose
  `plan_{name}_workflow` and `load_{name}_skill` MCP tools driven by the
  state graph in the server's tool catalog. No client-side planning
  package or middleware is required.
- **Local data lake file access** — once you have a local catalog working
  (see ["Run with a local-only data lake"](#run-with-a-local-only-data-lake)),
  wire `LocalFileFetcher` from
  [`code_execution/data_access/fetchers.py`](../../../src/code_execution/code_execution/data_access/fetchers.py)
  into your execution environment so the agent can actually read the files
  referenced by `storage_url` — no Azure storage credentials needed.
