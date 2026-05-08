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
  (run `az login` first), but the BYO-LLM factory in [llm.py](llm.py) also
  supports Azure OpenAI API keys, OpenAI, and Ollama — see Step A.
- `.env` populated at the repo root. Copy entries from
  [.env.example](.env.example) and the repo-level
  [.env.example](../../../.env.example) as needed.

**Optional (the script degrades gracefully if these are missing):**

- **Docker** — needed only for the chemistry MCP server (Step C). Without
  it, the agent runs with just the data lake tool.
- **Azure AI Search-backed data lake** — set `DATA_LAKE_SEARCH_ENDPOINT`
  to enable Step B. If unset, the script logs a skip message and runs
  with chemistry tools only.
- **Azure resources for tool-learning middleware** — only used by the
  optional [middleware variant](#optional-middleware-variant); see that
  section for details.

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

The conda environment is built into the chemistry image during
`docker compose up --build`, so the running container does not need to build
it again on first startup.

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

> **AOAI scope** — standard Azure OpenAI deployments use the scope
> `https://cognitiveservices.azure.com/.default`. Some internal/gateway
> endpoints require a different scope; set `AOAI_SCOPE` to whatever your
> endpoint owner specifies.

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

## Optional: middleware variant

[`agent_with_middleware.py`](agent_with_middleware.py) is a sibling script
that wires the same agent with three pieces of agora-workbench middleware.
It demonstrates the **framework-agnostic protocol + MAF adapter** pattern:
the middleware classes live in [`src/middleware/`](../../../src/middleware/)
and implement protocols defined in
[`src/middleware/protocols/`](../../../src/middleware/protocols/); a thin
adapter ([`maf_protocols.py`](../../../src/middleware/decision_log/adapters/maf_protocols.py))
wraps each one for use as a native MAF middleware or context provider.

```bash
uv run python docs/tutorials/maf_quickstart/agent_with_middleware.py
```

### What it adds on top of `agent.py`

| Step | Component | What it does |
| --- | --- | --- |
| **F** | [`DecisionLogChatMiddleware`](../../../src/middleware/decision_log/adapters/) | Observes every LLM round-trip and asynchronously synthesises a one-line "what did the agent decide" entry into a shared `DecisionLog`. |
| **F** | [`DecisionLogContextProvider`](../../../src/middleware/decision_log/adapters/) | Before each agent run, prepends the accumulated log as a `<decision_log>` system message so the agent can see its own history. Flushes the synthesis queue first so nothing is missed. |
| **G** | [`VignetteFunctionMiddleware`](../../../src/middleware/tool_learning/adapters/maf_function.py) | Wraps every tool call to (a) check anti-pattern guardrails before execution, and (b) attempt repair using stored vignettes when a tool call fails. Read-only by default (`write_vignettes=False`). |
| **D'** | `step_d_build_agent_with_middleware` | Re-builds the agent with the `middleware=` and `context_providers=` kwargs alongside `tools=`. Reuses the system prompt (including the injected `SKILL.md`) from `step_d_build_agent`. |

### Graceful degradation

Each middleware is optional and the script degrades cleanly:

- Step F always runs — `DecisionLogChatMiddleware` only needs the same chat
  client the agent already uses (it issues a small synthesis call per
  round-trip).
- Step G is skipped with an `INFO` log when both
  `TOOL_LEARNING_SEARCH_ENDPOINT` and `TOOL_LEARNING_TABLE_ENDPOINT` are
  unset. With only Search configured (`write_vignettes=False`) the
  pre-call guardrail and post-failure repair paths still work; Azure
  Tables is only required when `write_vignettes=True`. Tracking issue:
  [#77](https://github.com/microsoft/agora-workbench/issues/77).
- Steps B and C degrade exactly as in [agent.py](agent.py).

### What you'll see in a successful run

After the usual `AGENT:` block, the script flushes pending synthesis with
`await chat_mw.flush()` and prints the captured log:

```
======================================================================
DECISION LOG (captured by DecisionLogChatMiddleware)
======================================================================
[2026-05-08T16:48:37Z] chem_quickstart_agent: Searched the data lake for
chemistry datasets and evaluated four molecules for Lipinski compliance,
finding three passes and atorvastatin failing.
  Evidence: search_query=chemistry, screening_rule=Lipinski drug-likeness,
  pass_molecules=aspirin, caffeine, ibuprofen, fail_molecule=atorvastatin,
  failure_reasons=MW = 558.65 (>= 500); LogP = 6.31 (>= 5)
```

One entry per LLM round-trip; expect 2–3 per run.

### Why this pattern matters

Because the protocols ([`ChatMiddleware`](../../../src/middleware/protocols/middleware.py),
`FunctionMiddleware`, `ContextProvider`) are framework-agnostic, the same
`DecisionLogChatMiddleware` instance can be plugged into any agent runtime
that implements the protocol — only the adapter changes. This keeps
agora-workbench middleware portable across MAF, Semantic Kernel, custom
loops, etc.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `ValueError: Environment variable 'AZURE_OPENAI_ENDPOINT' is required` | `.env` not loaded or missing the key. Check the repo-root `.env`. |
| `Step C: chemistry MCP server unreachable at http://localhost:8020/health` | Docker container not running. `docker compose up -d` in `src/domain_examples/chemistry/`. |
| `azure.identity` errors / 401s on data lake search | `az login` expired — re-authenticate. |
| AOAI 403 / "scope not allowed" | `AOAI_SCOPE` doesn't match the endpoint. Standard AOAI uses `https://cognitiveservices.azure.com/.default`; some internal/gateway endpoints require a different scope — check with your endpoint owner. |
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
- **Middleware** — see the [Optional: middleware variant](#optional-middleware-variant)
  section above for `DecisionLogChatMiddleware`,
  `DecisionLogContextProvider`, and `VignetteFunctionMiddleware`.
- **Local data lake** — when [PR #67](https://github.com/microsoft/agora-workbench/pull/67)
  lands, swap `DefaultDataLakeSearchBackend` for `LocalDataLakeSearchBackend`
  and run fully offline.
