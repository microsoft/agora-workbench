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
directly. The chemistry server (see
[chemistry_server.py](../../../src/domain_examples/chemistry/server/chemistry_server.py))
exposes a small set of MCP tools:

- `execute_chemistry_code` — run Python in a long-lived Jupyter kernel with
  RDKit pre-imported (`Chem`, `Descriptors`, `AllChem`, `rdMolDescriptors`,
  `np`, `pd`).
- `list_chemistry_domain_tools` — discovery tool that prints the catalog of
  typed helpers below.
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
INFO maf_quickstart: Step A: built chat client AzureOpenAIChatClient
INFO maf_quickstart: Step B: built data lake search tool
INFO maf_quickstart: Step C: built chemistry MCP tool @ http://localhost:8020/mcp
INFO maf_quickstart: Step D: built agent with 2 tool(s); skill injected: True

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

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `ValueError: Environment variable 'AZURE_OPENAI_ENDPOINT' is required` | `.env` not loaded or missing the key. Check the repo-root `.env`. |
| `Step C: chemistry MCP server unreachable at http://localhost:8020/health` | Docker container not running. `docker compose up -d` in `src/domain_examples/chemistry/`. |
| `azure.identity` errors / 401s on data lake search | `az login` expired — re-authenticate. |
| TRAPI 403 / "scope not allowed" | `AOAI_SCOPE` doesn't match the endpoint. Use `api://trapi/.default` for TRAPI, `https://cognitiveservices.azure.com/.default` for standard AOAI. |
| `NameError: name 'parse_molecule' is not defined` (or any typed helper) inside `execute_chemistry_code` | The `chemistry_tools` package failed to install into the conda env at server-build time (look for `Additional command failed (continuing anyway)` in the container logs). Workaround: `docker exec <chem-container> /home/appuser/.cache/mcp-envs/chemistry/conda/bin/python -m pip install --no-deps /app/domain_examples/chemistry/chemistry_tools`. |

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
