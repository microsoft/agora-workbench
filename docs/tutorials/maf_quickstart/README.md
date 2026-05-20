# MAF + agora-workbench Quickstart

Wire a [Microsoft Agent Framework](https://github.com/microsoft/agent-framework)
agent to two agora-workbench domain MCP servers and watch it answer a
chemistry + power-systems question end-to-end.

## What you'll build

A single MAF agent with two MCP toolsets:

```
                         ┌──────────────────────────┐
              ┌──────────┤  chemistry MCP toolset   │  ← local Docker
              │          │  (MCPStreamableHTTPTool) │     :8020/mcp
              │          └──────────────────────────┘
              │               │
              │               ├─ execute_chemistry_code   (Python kernel + RDKit)
              │               ├─ search_chemistry_tools   (server-side BM25)
              │               └─ session / job helpers
   MAF Agent ─┤
              │          ┌──────────────────────────┐
              └──────────┤  energy systems MCP set  │  ← local Docker
                         │  (MCPStreamableHTTPTool) │     :8022/mcp
                         └──────────────────────────┘
                              │
                              ├─ execute_energysystems_code  (Python kernel + PyPSA)
                              ├─ search_energysystems_tools  (server-side BM25)
                              └─ session / job helpers
```

The agent receives a prompt asking for two tasks: screen a small molecule
library for Lipinski drug-likeness, then build and dispatch a tiny 2-bus
power grid. It chains the typed helpers in each domain (auto-injected into
the kernel namespace) and reports the results. Each domain server ships a
[`SKILL.md`](../../../src/domain_examples/chemistry/skills/SKILL.md) workflow
guide which is injected into the system prompt so the agent uses tools in
the recommended order.

> **Tool search and indexing are server-side.** Each MCP server builds a
> BM25 index over its own `ToolRegistry` at startup and exposes it as an
> MCP tool. No client-side search infrastructure, middleware, or planning
> package is required.

> **Data catalog — currently a placeholder.** The repo includes a
> server-side catalog package
> ([`catalog_tools.py`](../../../src/code_execution/code_execution/catalog_tools.py))
> that auto-registers `search_data`, `get_artifact`, and `list_domains`
> MCP tools when a server is launched with a `catalog.yaml` (see
> [`src/code_execution/catalog.example.yaml`](../../../src/code_execution/catalog.example.yaml)).
> The bundled chemistry and energy systems servers don't yet wire it in,
> so `step_b_data_lake_tool` in this tutorial is a no-op log — kept as a
> numbered step for forward-compatibility with future quickstarts.

## Prerequisites

**Required:**

- [`uv`](https://github.com/astral-sh/uv) installed.
- An LLM you can call. The default path is **Azure OpenAI via Entra ID**
  (run `az login` first), but the BYO-LLM factory in
  [chat_client.py](chat_client.py) also supports Azure OpenAI API keys,
  OpenAI, and Ollama — see Step A.
- `.env` populated at the repo root. Copy entries from
  [.env.example](.env.example) and the repo-level
  [.env.example](../../../.env.example) as needed.
- **Docker** — both MCP servers run as local containers.

## Setup

### 1. Configure environment

Copy the tutorial's [.env.example](.env.example) into your repo-root `.env`
(or merge missing keys). The default and tested LLM path is **Azure OpenAI
via Entra ID**.

### 2. Build the shared MCP base image (once)

Both domain servers inherit from this image:

```bash
cd src
docker build -f deployment/mcp_server/base.Dockerfile -t mcp-server-base:local .
```

### 3. Start the chemistry MCP server

> **⚠️ Local-dev auth only** — the bundled server uses
> [`create_noop_auth_config()`](../../../src/code_execution/code_execution/auth/),
> which accepts any bearer token (the tutorial sends a dummy
> `Authorization: Bearer dev-token`). It binds to `127.0.0.1:8020` so it's
> not reachable from outside the host. **Do not deploy this configuration
> to a publicly reachable server.**

```bash
cd src/domain_examples/chemistry
docker compose up -d --build
curl http://localhost:8020/health
# => {"status":"healthy", ...}
```

The first start takes a few minutes because the conda environment is built
from scratch with RDKit; subsequent starts are fast.

### 4. Start the energy systems MCP server

Same pattern, binds to `127.0.0.1:8022`:

```bash
cd src/domain_examples/energysystems
docker compose up -d --build
curl http://localhost:8022/health
# => {"status":"healthy", ...}
```

### 5. Run the agent

From the repo root:

```bash
uv run python docs/tutorials/maf_quickstart/agent.py
```

You should see log lines for each step (chat client, data lake placeholder,
chemistry tool, energy systems tool, agent build), then the agent's final
answer with descriptors for each molecule and OPF results for the 2-bus
grid.

## Walkthrough

The runnable script is [agent.py](agent.py); each integration point is its
own function so you can map README sections to code.

### Step A — Build the chat client (BYO LLM)

[`step_a_chat_client`](agent.py) calls `build_chat_client()` from
[chat_client.py](chat_client.py). agora-workbench is **BYO LLM**: any
object that satisfies MAF's `ChatClient` protocol works. The tutorial
factory is a thin wrapper around the framework-agnostic
[`ModelSpec`](../../../src/llm/spec.py) +
[`make_maf_client`](../../../src/llm/factories/maf.py) abstraction in
`src/llm/`, and dispatches on `$LLM_PROVIDER`:

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

The Entra path delegates to
[`auth.providers.get_token_provider()`](../../../src/auth/providers.py),
which returns a callable backed by the same
`AzureCliCredential → ManagedIdentityCredential` chain used everywhere
else in the repo. No new credentials are needed.

> **AOAI scope** — standard Azure OpenAI deployments use
> `https://cognitiveservices.azure.com/.default`. Some internal/gateway
> endpoints require a different scope; set `AOAI_SCOPE` to whatever your
> endpoint owner specifies.

> **API version on the Responses API** — `OpenAIChatClient` calls the
> `/responses` endpoint. Public Azure OpenAI accepts the usual dated
> previews (e.g. `2025-04-01-preview`). Some internal gateways only
> accept the floating tags `preview` or `v1` on `/responses` and return
> `BadRequest: API version not supported` for dated strings — if you see
> that, set `API_VERSION="preview"`.

### Step B — Data catalog (server-side placeholder)

[`step_b_data_lake_tool`](agent.py) is intentionally a no-op log. Data
catalog search has moved into the MCP server itself: when a server is
launched with a `catalog.yaml` (see
[`src/code_execution/catalog.example.yaml`](../../../src/code_execution/catalog.example.yaml)),
[`register_catalog_tools`](../../../src/code_execution/code_execution/catalog_tools.py)
indexes the declared sources on startup and exposes `search_data`,
`get_artifact`, and `list_domains` as MCP tools. The agent discovers them
automatically through the `MCPStreamableHTTPTool` connection — no
client-side adapter is needed.

The bundled chemistry and energy systems example servers in this tutorial
don't ship a `catalog.yaml`, so no catalog tools are registered today.
The step is kept in `agent.py` as a forward-compatible hook for when you
configure a catalog on one of your servers.

### Step C — Chemistry MCP tool

[`step_c_chemistry_tool`](agent.py) instantiates
`MCPStreamableHTTPTool(name="chemistry", url=..., tool_name_prefix="chem_", approval_mode="never_require")`
pointing at `http://localhost:8020/mcp`. The chemistry server (see
[chemistry_server.py](../../../src/domain_examples/chemistry/server/chemistry_server.py))
exposes:

- `execute_chemistry_code` — run Python in a long-lived Jupyter kernel
  with RDKit pre-imported (`Chem`, `Descriptors`, `AllChem`,
  `rdMolDescriptors`, `np`, `pd`).
- `search_chemistry_tools` — **server-side** BM25 search over the
  domain's typed helper catalog. The server builds the index from its own
  `ToolRegistry` at startup, so no client-side indexing infrastructure is
  needed. Call with `(query: str, top: int = 5)`; pass `query=""` with
  `top=999` to enumerate the full catalog. Each result includes `name`,
  `description`, `execution_type`, `score`, `state_requires`, and
  `state_produces`.
- `check_job`, `chemistry_*` (sessions, parallel execute, push object) —
  session / lifecycle helpers.

The `tool_name_prefix="chem_"` is what lets a single agent talk to two
domain servers without name collisions — every tool the chemistry server
exposes appears to the LLM as `chem_<original_name>`.

**Typed domain helpers** are *not* separate MCP tools. They live in the
[`chemistry_tools`](../../../src/domain_examples/chemistry/chemistry_tools/)
pip package, which is installed into the kernel's conda env at server
build time. The server then auto-injects an instrumented Python proxy
for each helper into the kernel namespace via
[`tool_proxy.py`](../../../src/code_execution/code_execution/tool_proxy.py),
so inside `execute_chemistry_code` the agent can simply call them as
plain Python functions — no imports required:

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

A `list_tools()` function is also injected into the kernel namespace if
the agent needs to enumerate them at runtime.

The tool probes `/health` before connecting; if the server isn't running
you get a clean skip message instead of a confusing error inside the
agent loop.

### Step C2 — Energy systems MCP tool

[`step_c2_energysystems_tool`](agent.py) mirrors Step C against
`http://localhost:8022/mcp` with `tool_name_prefix="energy_"`. The
energy systems server exposes the same MCP tool shape as chemistry, just
backed by [PyPSA](https://pypsa.org/):

- `execute_energysystems_code` — Python kernel with `pypsa`, `np`, `pd`,
  `nx`, `plt` pre-imported.
- `search_energysystems_tools` — server-side BM25 search over the energy
  systems typed helper catalog.
- session / job helpers.

| Helper | Purpose |
| --- | --- |
| `define_network` | Create a named PyPSA `Network` with snapshots |
| `add_components` | Attach buses, generators, loads, lines, storage units |
| `add_time_series` | Bulk-attach per-snapshot generator/load profiles |
| `run_power_flow` | Nonlinear AC / linear DC power flow |
| `run_optimal_power_flow` | Economic dispatch (HiGHS) |
| `run_capacity_expansion` | Joint generation + storage sizing |
| `analyze_costs` | Per-generator and total cost breakdown |
| `analyze_topology` | Graph metrics and connectivity checks |

### Step D — Build the agent

[`step_d_build_agent`](agent.py) reads each domain's
[`SKILL.md`](../../../src/domain_examples/chemistry/skills/SKILL.md) — a
portable workflow guide that documents the tool state-graph, default
parameters, and common pitfalls — and appends both to the system prompt.
This is the simplest version of the agora-workbench *skills* pattern:
domain knowledge travels with the domain instead of being hard-coded
into the agent. The MAF agent is then built with a single call:

```python
agent = chat_client.as_agent(
    name="quickstart_agent",
    instructions=...,  # base instructions + injected chemistry + energy SKILL.md
    tools=tools,
)
```

### Step E — Run a single turn

[`step_e_run`](agent.py) sends one prompt and prints the response text.
The prompt asks for two tasks in **strict sequential order**: finish
chemistry first, then energy systems. MAF handles the tool-calling loop
(LLM emits a tool call → MAF dispatches to the right MCP server →
result feeds back into the conversation → repeat until done).

> **Why sequential?** When the prompt invites two independent tool calls
> in one turn, the Responses API will dispatch them in **parallel** to
> the two MCP servers. As of `agent-framework` 1.2 / `MCPStreamableHTTPTool`,
> concurrent calls across two distinct servers can drop the second
> response — the second tool's result never streams back to the client
> and the agent loop hangs. Forcing one-at-a-time execution in the
> prompt is the current workaround.

## Expected output

A successful run looks roughly like:

```
INFO maf_quickstart: Step A: built chat client OpenAIChatClient
INFO maf_quickstart: Step B: data catalog tools (search_data, get_artifact, list_domains) are auto-discovered from any MCP server configured with a catalog.yaml.
INFO maf_quickstart: Step C: built chemistry MCP tool @ http://localhost:8020/mcp
INFO maf_quickstart: Step C2: built energy systems MCP tool @ http://localhost:8022/mcp
INFO maf_quickstart: Step D: built agent with 2 tool(s); skills injected: chemistry=True, energy=True

======================================================================
USER: Do TWO tasks, STRICTLY ONE AT A TIME … TASK 1 — Chemistry … TASK 2 — Energy Systems …
======================================================================

AGENT:
TASK 1 — Chemistry (Lipinski + descriptors):
  - aspirin       → PASS  (MW ≈ 180.16, LogP ≈ 1.31)
  - caffeine      → PASS  (MW ≈ 194.19, LogP ≈ -1.03)
  - ibuprofen     → PASS  (MW ≈ 206.28, LogP ≈ 3.07)
  - atorvastatin  → FAIL  (MW ≈ 558.65 > 500, LogP ≈ 6.31 > 5)

TASK 2 — Energy Systems (2-bus OPF):
  Objective cost: 36000
  Dispatch:       wind 150 MW, coal 50 MW
  Line loading:   25 % (50 MW on 200 MVA line)
  Marginal price: 30 / MWh at both buses
```

(Exact values may differ slightly run-to-run depending on which RDKit /
PyPSA versions the server environments resolve.)

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `ValueError: Environment variable 'AZURE_OPENAI_ENDPOINT' is required` | `.env` not loaded or missing the key. Check the repo-root `.env`. |
| `ImportError: cannot import name 'AzureOpenAIChatClient' from 'agent_framework.azure'` | You're on `agent-framework >= 1.2`, which removed that class. The tutorial's [chat_client.py](chat_client.py) already targets the unified `agent_framework.openai.OpenAIChatClient`; if you've forked or pinned to an older version, either update or pin `agent-framework<1.2`. |
| `BadRequest: API version not supported` from `/responses` | The Responses API on your endpoint doesn't accept the configured `API_VERSION`. Try `API_VERSION="preview"` (some internal gateways only accept floating tags; public AOAI typically wants a dated preview like `2025-04-01-preview`). |
| `404 DeploymentId Not Found` | The deployment id doesn't exist on your endpoint. Internal gateways often require dated ids like `gpt-5.2-codex_2026-01-14`. |
| `Bind for 127.0.0.1:8020 failed: port is already allocated` | A previous container (or unrelated process) is still holding the port. Find it with `docker ps \| grep 8020` and remove with `docker rm -f <name>`, then retry `docker compose up -d`. (Same for `:8022`.) |
| Container exits immediately with `Could not resolve host: conda.anaconda.org` | Transient DNS / network blip while the conda env is being built on first start. Retry: `docker compose down && docker compose up -d`. |
| `Step C: chemistry MCP server unreachable at http://localhost:8020/health` | Docker container not running. `docker compose up -d` in `src/domain_examples/chemistry/`. |
| `Step C2: energy systems MCP server unreachable at http://localhost:8022/health` | Same as above but in `src/domain_examples/energysystems/`. |
| AOAI 403 / "scope not allowed" | `AOAI_SCOPE` doesn't match the endpoint. Standard AOAI uses `https://cognitiveservices.azure.com/.default`; some internal/gateway endpoints require a different scope — check with your endpoint owner. |
| Agent hangs after one tool call returns | Parallel tool-call streaming bug across two MCP servers (see note in Step E). Make sure the prompt forces strict sequential execution. |
| Container exits during startup with `RuntimeError: Additional command 1/1 failed` | A pip-install step inside the conda env failed; the build now surfaces this instead of silently continuing. Read the surrounding container logs for the underlying pip error (network, missing build dep, etc.) and rebuild with `docker compose up --build`. |

## Cleanup

```bash
cd src/domain_examples/chemistry && docker compose down
cd src/domain_examples/energysystems && docker compose down
```

## Next steps

This quickstart deliberately keeps the surface small. Once you've got it
working, layer in:

- **Server-side data catalog** — write a `catalog.yaml` for one of your
  servers (template:
  [`src/code_execution/catalog.example.yaml`](../../../src/code_execution/catalog.example.yaml))
  and wire it into the server entry point via
  [`register_catalog_tools`](../../../src/code_execution/code_execution/catalog_tools.py).
  Once registered, the agent automatically picks up `search_data`,
  `get_artifact`, and `list_domains` — no changes to `agent.py` required.
- **Workflow planning** — servers with state-annotated tools also expose
  `plan_{name}_workflow` and `load_{name}_skill` MCP tools driven by the
  state graph in the server's tool catalog. No client-side planning
  package or middleware is required.
- **Multi-turn chat** — switch from the single-shot `agent.py` to the
  REPL in [chat.py](chat.py) (same setup, looped `agent.run(...)` with
  history).
