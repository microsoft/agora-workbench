# OpenAI Agents + agora-workbench Quickstart

Wire an [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/)
agent to the agora-workbench chemistry MCP server and watch it answer a
drug-likeness question end-to-end.

This tutorial is the openai-agents counterpart to
[`docs/tutorials/maf_quickstart/`](../maf_quickstart/) and is intentionally
diff-able with it: same numbered steps, same chemistry MCP server, same
prompt shape. Use it to compare how the two L6 frameworks express the same
agent loop.

## What you'll build

```
              ┌──────────────────────────────────┐
              │  openai-agents Agent             │
              │  (model = OpenAIResponsesModel)  │
   You ──────►│                                  │
              │  mcp_servers = [                 │
              │    MCPServerStreamableHttp(...)  ├─────► chemistry MCP server
              │  ]                               │       (local Docker :8020)
              └──────────────────────────────────┘
                                                          │
                                                          ├─ execute_chemistry_code   (Python kernel + RDKit)
                                                          ├─ search_chemistry_tools   (server-side BM25)
                                                          └─ session / job helpers
```

The agent receives one prompt — "screen this small library of molecules
for drug-likeness" — and chains the typed chemistry helpers
(`filter_drug_candidates`, `compute_descriptors`, …) auto-injected into the
MCP server's Python kernel. The chemistry domain's
[`SKILL.md`](../../../examples/domain_examples/chemistry/skills/SKILL.md) is
injected into the system prompt so the agent uses tools in the recommended
order.

> **Tool search and indexing are server-side.** The chemistry server builds
> a BM25 index over its own `ToolRegistry` at startup and exposes it as
> `search_chemistry_tools`. No client-side search infrastructure or
> middleware is required.

> **Data catalog — placeholder.** When an MCP server is launched with a
> `catalog.yaml` (see
> [`src/code_execution/catalog.example.yaml`](../../../src/code_execution/catalog.example.yaml)),
> [`register_catalog_tools`](../../../src/code_execution/catalog_tools.py)
> auto-registers `search_data` / `get_artifact` / `list_domains`. The
> bundled chemistry server doesn't ship a `catalog.yaml`, so
> `step_b_data_lake_tool` in this tutorial is a no-op log — kept as a
> forward-compatible numbered step.

## Prerequisites

**Required:**

- [`uv`](https://github.com/astral-sh/uv) installed.
- `openai-agents` available. Either install the optional extra:
  ```bash
  uv add 'agora-workbench[openai-agents]'
  ```
  or install it directly into your environment:
  ```bash
  uv pip install openai-agents
  ```
- An LLM you can call. Default is **Azure OpenAI via Entra ID** (run
  `az login` first). See [Step A](#step-a--build-the-model-byo-llm) for the
  full provider table.
- `.env.agent` populated at the repo root. Copy entries from
  [.env.agent.example](.env.agent.example) as needed.
- **Docker** — the chemistry MCP server runs as a local container.

## Setup

### 1. Configure environment

Copy [.env.agent.example](.env.agent.example) to `.env.agent` at the repo
root and fill in values. The default and tested LLM path is **Azure OpenAI
via Entra ID**.

### 2. Build the shared MCP base image (once)

```bash
docker build -f deployment/base.Dockerfile -t mcp-server-base:local .
```

### 3. Start the chemistry MCP server

> **⚠️ Local-dev auth only** — the bundled server uses
> [`create_noop_auth_config()`](../../../src/code_execution/auth/),
> which accepts any bearer token (the tutorial sends a dummy
> `Authorization: Bearer dev-token`). It binds to `127.0.0.1:8020` so it's
> not reachable from outside the host. **Do not deploy this configuration
> to a publicly reachable server.**

```bash
cd examples/domain_examples/chemistry
docker compose up -d --build
curl http://localhost:8020/health
# => {"status":"healthy", ...}
```

The first start takes a few minutes (conda env with RDKit); subsequent
starts are fast.

### 4. Run the agent

From the repo root:

```bash
uv run python docs/tutorials/openai_agents_quickstart/agent.py
```

You should see log lines for each step, then the agent's final answer with
Lipinski pass/fail + MW + LogP for each of the four test molecules.

## Walkthrough

The runnable script is [agent.py](agent.py); each integration point is its
own function. Compare side-by-side with
[`docs/tutorials/maf_quickstart/agent.py`](../maf_quickstart/agent.py) — the
step boundaries line up.

### Step A — Build the model (BYO LLM)

[`step_a_chat_client`](agent.py) calls `build_model()` from
[chat_client.py](chat_client.py), which dispatches on `$LLM_PROVIDER`.
Unlike MAF (which has a single `OpenAIChatClient` for every backend), the
openai-agents SDK accepts several different things for `Agent(model=...)`:

| `LLM_PROVIDER` | Returns | Notes |
| --- | --- | --- |
| `azure_openai_entra` *(default)* | `OpenAIResponsesModel` wrapping `openai.AsyncAzureOpenAI(azure_ad_token_provider=...)` | Auth via the same `get_token_provider` chain (AzureCli → ManagedIdentity) — see [`src/code_execution/auth/azure_credentials.py`](../../../src/code_execution/auth/azure_credentials.py). Calls `/responses`. Set `AZURE_OPENAI_API_KIND=chat_completions` to fall back to `/chat/completions`. |
| `openai` | bare model id string (e.g. `"gpt-4o"`) | The SDK picks up `OPENAI_API_KEY` from the env. |

For Azure-API-key, Ollama, or LiteLLM, see the
[MAF quickstart](../maf_quickstart/chat_client.py) — `agent_helpers.llm`
already supports them; the openai-agents factory keeps the surface small
and only exercises the tested Azure-Entra and public-OpenAI paths.

Under the hood the factory delegates to
[`ModelSpec.from_env(...)`](../../../agent_helpers/llm/spec.py) for credential
and config resolution — same code path the MAF quickstart uses, just with
a different per-framework adapter on the back end.

> **Responses vs Chat Completions** — we default to
> `OpenAIResponsesModel` (`/responses`) because the dated Azure deployments
> we test against (`gpt-5.2-codex_*`, `gpt-5.1_*`) are Responses-only and
> return a clean 404 on `/chat/completions`. If your endpoint doesn't
> expose `/responses`, set `AZURE_OPENAI_API_KIND=chat_completions` in
> `.env.agent` to switch to `OpenAIChatCompletionsModel`.

> **AOAI scope** — standard Azure OpenAI deployments use
> `https://cognitiveservices.azure.com/.default`. Some internal/gateway
> endpoints require a different scope; set `AOAI_SCOPE` in `.env`.

### Step B — Data catalog (server-side placeholder)

[`step_b_data_lake_tool`](agent.py) is a no-op log, same as in the MAF
quickstart. Data catalog tools are now auto-registered by any MCP server
launched with a `catalog.yaml`; the bundled chemistry server doesn't ship
one, so no catalog tools are advertised today. Kept as a numbered step for
forward-compat with future quickstarts.

### Step C — Chemistry MCP server

[`step_c_chemistry_tool`](agent.py) builds an
`MCPServerStreamableHttp(name="chemistry", params={"url": ..., "headers": {...}}, cache_tools_list=True)`
pointed at `http://localhost:8020/mcp`. Key differences from the MAF
adapter:

| | MAF (`MCPStreamableHTTPTool`) | OpenAI Agents (`MCPServerStreamableHttp`) |
| --- | --- | --- |
| Module | `agent_framework` | `agents.mcp` |
| URL / headers | direct kwargs | inside `params={"url":..., "headers":...}` |
| Cached tool list | always on | opt-in via `cache_tools_list=True` |
| Tool name prefix | `tool_name_prefix=` | `Agent.mcp_config={"include_server_in_tool_names": True}` |
| Attach to agent | `tools=[tool]` | `mcp_servers=[server]` |
| Session lifetime | `async with tool:` | `async with server:` |

The same chemistry MCP tools are exposed to either framework:

- `execute_chemistry_code` — Python kernel with RDKit pre-imported
  (`Chem`, `Descriptors`, `AllChem`, `rdMolDescriptors`, `np`, `pd`).
- `search_chemistry_tools` — server-side BM25 search over the typed helper
  catalog. Call with `(query: str, top: int = 5)`; pass `query=""` with
  `top=999` to enumerate.
- session / job helpers.

**Typed domain helpers** are *not* separate MCP tools — they're installed
into the kernel's conda env and auto-injected as Python proxies by
[`tool_proxy.py`](../../../src/code_execution/tool_proxy.py).
Inside `execute_chemistry_code` the agent calls them as plain functions:

```python
result = filter_drug_candidates(
    ["CC(=O)OC1=CC=CC=C1C(=O)O", "CN1C=NC2=C1C(=O)N(C(=O)N2C)C"],
    rules="lipinski",
)
print(result)
```

The tool probes `/health` before connecting; if the server isn't running
you get a clean skip message instead of a confusing error inside the
runner loop.

### Step D — Build the agent

[`step_d_build_agent`](agent.py) reads the chemistry domain's
[`SKILL.md`](../../../examples/domain_examples/chemistry/skills/SKILL.md),
strips the YAML frontmatter, and appends it to the system prompt. The
agent is then built with a single call:

```python
agent = Agent(
    name="chem_quickstart_agent",
    instructions=...,        # base instructions + injected SKILL.md
    model=model,             # from step_a
    mcp_servers=mcp_servers, # from step_c
)
```

Note the symmetry with the MAF quickstart's `chat_client.as_agent(...)` —
the agora-workbench *skills* pattern (domain knowledge travels with the
domain) is framework-agnostic by design.

### Step E — Run a single turn

[`step_e_run`](agent.py) sends one prompt to `Runner.run(agent, prompt)`
and prints `result.final_output`. The SDK handles the tool-calling loop
(LLM emits a tool call → SDK dispatches to the MCP server → result feeds
back → repeat until done).

## Expected output

A successful run looks roughly like:

```
INFO openai_agents_quickstart: Step A: built openai-agents model OpenAIResponsesModel
INFO openai_agents_quickstart: Step B: data catalog tools ... are auto-discovered ...
INFO openai_agents_quickstart: Step C: built chemistry MCP server @ http://localhost:8020/mcp
INFO openai_agents_quickstart: Step D: built Agent with 1 MCP server(s); skill injected: True

======================================================================
USER: Screen this small library of molecules for drug-likeness ...
======================================================================

AGENT:
Lipinski drug-likeness screen:
  - aspirin       → PASS  (MW ≈ 180.16, LogP ≈ 1.31)
  - caffeine      → PASS  (MW ≈ 194.19, LogP ≈ -1.03)
  - ibuprofen     → PASS  (MW ≈ 206.28, LogP ≈ 3.07)
  - atorvastatin  → FAIL  (MW ≈ 558.65 > 500, LogP ≈ 6.31 > 5)
```

(Exact values may differ slightly run-to-run depending on which RDKit
version the chemistry environment resolves.)

## Multi-turn chat

For a back-and-forth session, run [`chat.py`](chat.py) instead. Same setup
functions, but the loop threads `previous_response_id` through
`Runner.run(...)` so the agent remembers prior turns:

```bash
uv run python docs/tutorials/openai_agents_quickstart/chat.py
```

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `ModuleNotFoundError: No module named 'agents'` | `openai-agents` not installed in this env. `uv add 'agora-workbench[openai-agents]'` or `uv pip install openai-agents`. |
| `ValueError: Environment variable 'AZURE_OPENAI_ENDPOINT' is required` | `.env` not loaded or missing the key. Check the repo-root `.env`. |
| `BadRequestError: Invalid value for 'model'` / `model_not_found` | The deployment id doesn't exist on your endpoint. Internal gateways often require dated ids like `gpt-5.2-codex_2026-01-14`. |
| `NotFoundError` on `/responses` | The endpoint doesn't support the Responses API. Set `AZURE_OPENAI_API_KIND=chat_completions` in `.env.agent` to switch to `OpenAIChatCompletionsModel`. |
| `NotFoundError` on `/chat/completions` | The deployment is Responses-only (e.g. `gpt-5.2-codex_*`). Leave `AZURE_OPENAI_API_KIND` unset (default is Responses) or point at a chat-completions deployment. |
| `azure.identity` errors / 401s | `az login` expired — re-authenticate. |
| AOAI 403 / "scope not allowed" | `AOAI_SCOPE` doesn't match the endpoint. Standard AOAI uses `https://cognitiveservices.azure.com/.default`; some internal/gateway endpoints require a different scope — check with your endpoint owner. |
| `Step C: chemistry MCP server unreachable at http://localhost:8020/health` | Docker container not running. `docker compose up -d` in `examples/domain_examples/chemistry/`. |
| `Bind for 127.0.0.1:8020 failed: port is already allocated` | A previous container (or unrelated process) is still holding the port. Find with `docker ps \| grep 8020` and remove with `docker rm -f <name>`. |
| Tracing chatter to `api.openai.com` | OAI Agents SDK uploads traces by default. Set `OPENAI_AGENTS_DISABLE_TRACING=1` to keep tracing fully off-host. |

## Cleanup

```bash
cd examples/domain_examples/chemistry && docker compose down
```

## Next steps

- **Compare with MAF** — diff [agent.py](agent.py) against
  [`docs/tutorials/maf_quickstart/agent.py`](../maf_quickstart/agent.py) to
  see exactly where the two frameworks differ. The setup functions are
  intentionally parallel.
- **Add a second MCP server** — wire the energy systems server (`:8022`)
  by appending a second `MCPServerStreamableHttp` to `mcp_servers`. The
  OAI SDK does not have the
  [parallel-tool-call streaming bug](../maf_quickstart/README.md#step-e--run-a-single-turn)
  that the MAF quickstart documents.
- **Sessions / threads** — switch from `previous_response_id=...` to
  `agents.SQLiteSession(...)` (or any of the other built-in session
  backends) for fully persistent multi-turn history.
- **Tool deferred-loading** — for large tool surfaces, layer in the SDK's
  built-in `ToolSearchTool()` / `tool_namespace(...)` so the model can
  search-and-then-call instead of receiving every tool schema upfront.
- **Bring up the agora-workbench adapter** — once the adapter PR sequence
  lands, swap the local `chat_client.py` for `make_openai_agents_model`
  and the inline MCP setup for the shared
  `tools.mcp.adapters.openai_agents` adapter.
