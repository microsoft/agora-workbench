# GitHub Copilot SDK + agora-workbench Quickstart

Wire a [GitHub Copilot SDK](https://github.com/github/copilot-sdk/tree/main/python)
session to the agora-workbench energy systems MCP server and watch it
solve a small economic-dispatch problem end-to-end.

This is the Copilot SDK counterpart to
[`docs/tutorials/maf_quickstart/`](../maf_quickstart/) and
[`docs/tutorials/openai_agents_quickstart/`](../openai_agents_quickstart/),
but it deliberately does **not** mirror their file layout. The Copilot
CLI *is* the agent — there is no chat client, no `Agent` object, no
`Runner` — so the tutorial collapses to a single short
[agent.py](agent.py) that matches the SDK's own
[Quick Start](https://github.com/github/copilot-sdk/blob/main/python/README.md#quick-start).

## What you'll build

```
              ┌──────────────────────────────────┐
              │  CopilotClient                   │
   You ──────►│   ↳ create_session(              │
              │       mcp_servers={...},         ├─────► energysystems MCP server
              │       system_message={...},      │       (local Docker :8022)
              │     )                            │
              └──────────────────────────────────┘
                                                          │
                                                          ├─ execute_energysystems_code  (Python kernel + PyPSA)
                                                          ├─ search_energysystems_tools  (server-side BM25)
                                                          └─ session / job helpers
```

The session receives one prompt — a 3-bus economic dispatch problem
with a deliberately tight transmission line — and chains the typed
energysystems helpers
(`define_network`, `add_components`, `run_optimal_power_flow`,
`analyze_costs`, …) auto-injected
into the MCP server's Python kernel. The energy systems domain's
[`SKILL.md`](../../../examples/domain_examples/energysystems/skills/SKILL.md)
is appended to the SDK's default system prompt so the agent uses tools in
the recommended order.

## Mapping to the sister quickstarts

If you've read the MAF or openai-agents tutorials, here's where each
numbered step ends up in this one:

| Sister step | Copilot SDK equivalent | Lives in |
| --- | --- | --- |
| A. Build chat client | `resolve_llm()` — returns `(model, provider)`; `provider=None` for the logged-in subscription | [agent.py](agent.py) |
| B. Data catalog | n/a — auto-discovered server-side from `catalog.yaml`; the bundled energysystems server doesn't ship one | — |
| C. Build MCP tool object | A plain config `dict` passed as `mcp_servers={"energysystems": {...}}`; the CLI manages the transport | [`energy_mcp_config()`](agent.py) |
| D. Build agent | `create_session(mcp_servers=..., system_message=...)` — there is no separate `Agent` | inline in `main()` |
| E. Run a turn | `session.send_and_wait(prompt, timeout=...)` — the SDK owns the tool-call loop | inline in `main()` |

That's the whole framework comparison. The rest of this README is
setup + how to run.

## Prerequisites

- [`uv`](https://github.com/astral-sh/uv) installed.
- `github-copilot-sdk` available. Either install the optional extra:
  ```bash
  uv add 'agora-agent[copilot-sdk]'
  ```
  or install it directly into your environment:
  ```bash
  uv pip install github-copilot-sdk python-dotenv
  ```
  The package bundles the underlying Copilot CLI binary, so no separate
  install is needed.
- An LLM you can call. **Default is the logged-in Copilot subscription**
  — run `copilot auth login` once. BYOK alternatives below.
- **Docker** — the energysystems MCP server runs as a local container.
- Python 3.11+.

## Setup

### 1. (Optional) Configure BYOK

Skip this if you're using the default `copilot` provider. For BYOK, copy
[.env.agent.example](.env.agent.example) to `.env.agent` at the repo
root and fill in the section for your provider:

| `LLM_PROVIDER` | What you need | Notes |
| --- | --- | --- |
| `copilot` *(default)* | `copilot auth login` once | Override the default model with `COPILOT_MODEL`. |
| `azure_openai_key` | `AZURE_OPENAI_ENDPOINT` (host only), `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_DEPLOYMENT_NAME`, optional `API_VERSION` | `endpoint` is the **host only** (e.g. `https://my-resource.openai.azure.com`) — no `/openai/v1`. |
| `openai` | `OPENAI_API_KEY`, optional `OPENAI_MODEL`, `OPENAI_BASE_URL` | Works for public OpenAI and any OpenAI-compatible endpoint (Ollama, vLLM, …). |

> **No Entra ID** — the Copilot SDK's `ProviderConfig` only supports
> key-based auth. If your Azure endpoint is Entra-only, use the MAF or
> openai-agents quickstart instead.

### 2. Build the shared MCP base image (once)

```bash
docker build -f deployment/base.Dockerfile -t mcp-server-base:local .
```

### 3. Start the energy systems MCP server

> **⚠️ Local-dev auth only** — the bundled server uses
> [`create_noop_auth_config()`](../../../src/code_execution/auth/),
> which accepts any bearer token (the tutorial sends a dummy
> `Authorization: Bearer dev-token`). It binds to `127.0.0.1:8022`.
> **Do not deploy this configuration to a publicly reachable server.**

```bash
cd examples/domain_examples/energysystems
docker compose up -d --build
curl http://localhost:8022/health
# => {"status":"healthy", ...}
```

The first start takes a few minutes (conda env with PyPSA + HiGHS);
subsequent starts are fast.

### 4. Run the agent

From the repo root:

```bash
uv run python docs/tutorials/copilot_sdk_quickstart/agent.py
```

You should see a log line for the LLM resolution, then the agent's
final answer with the generator dispatch, line loadings, and total
system cost for the 3-bus network.

## How the script is shaped

[agent.py](agent.py) reads top-to-bottom:

```python
async def main() -> int:
    model, provider = resolve_llm()        # (1) pick model + optional BYOK
    energy = await energy_mcp_config()     # (2) build the MCP server dict
    if energy is None:
        return 1

    session_kwargs = {
        "model": model,
        "mcp_servers": {"energysystems": energy},
        "system_message": build_system_message(),  # (3) base + SKILL.md
    }
    if provider is not None:
        session_kwargs["provider"] = provider

    async with CopilotClient() as client:
        async with await client.create_session(
            on_permission_request=PermissionHandler.approve_all,
            **session_kwargs,
        ) as session:
            reply = await session.send_and_wait(PROMPT, timeout=300.0)
    ...
```

Notes:

- **System message uses `mode: "append"`.** That preserves the SDK's
  default safety / tool-use guardrails. Use `mode: "replace"` only if
  you have a reason to drop them.
- **`PermissionHandler.approve_all`** auto-approves every tool call —
  fine for a local quickstart, not what you want in production. See
  [Permission Handling](https://github.com/github/copilot-sdk/blob/main/python/README.md#permission-handling).
- **The MCP server is a `dict`, not a connection object.** The CLI
  manages the HTTP transport, so there's nothing for the tutorial to
  `async with`.
- **Typed domain helpers** (`define_network`, `add_components`,
  `run_optimal_power_flow`, `analyze_costs`, …) are *not* separate MCP
  tools — they're auto-injected into the kernel by
  [`tool_proxy.py`](../../../src/code_execution/tool_proxy.py) and
  called as plain Python inside `execute_energysystems_code`.

## Expected output

A successful run looks roughly like:

```
INFO copilot_sdk_quickstart: LLM: model=gpt-5.2, provider=copilot-subscription

======================================================================
USER: Solve an economic dispatch problem on a 3-bus PyPSA network ...
======================================================================

AGENT:
Economic dispatch on the 3-bus network:
  - g_cheap (b1, $20/MWh): 100 MW   ← capped by l_13
  - g_exp   (b2, $80/MWh): 100 MW
  - Line loadings:
      l_13: 100.0 MW / 100 MW   (100% — BINDING)
      l_23: 100.0 MW / 200 MW   ( 50%)
      l_12:  ~0  MW / 200 MW    (  0%)
  - Total system cost: $10,000/h  (100·20 + 100·80)

Why g_exp runs: with equal injections at b1 and b2 the cross-link
l_12 carries no flow by symmetry, so cheap power from b1 can only
reach the load through l_13. That line caps out at 100 MW, which in
turn caps g_cheap at 100 MW — the remaining 100 MW of demand has to
come from g_exp over l_23.
```

(Exact MW / cost values are deterministic for this LP, but the agent's
prose framing will vary across runs and models.)

## Multi-turn chat

[chat.py](chat.py) reuses the same three helpers (`resolve_llm`,
`energy_mcp_config`, `build_system_message`) and loops on stdin. The
session is created once and reused across turns, so history is preserved
automatically:

```bash
uv run python docs/tutorials/copilot_sdk_quickstart/chat.py
```

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `ModuleNotFoundError: No module named 'copilot'` / `ModuleNotFoundError: No module named 'dotenv'` | `github-copilot-sdk` (and `python-dotenv`) not installed. `uv add 'agora-agent[copilot-sdk]'` or `uv pip install github-copilot-sdk python-dotenv`. |
| CLI launch hangs / auth errors with `LLM_PROVIDER=copilot` | Logged-in Copilot subscription required. Run `copilot auth login` once. |
| `KeyError: 'AZURE_OPENAI_ENDPOINT'` | Switched to `LLM_PROVIDER=azure_openai_key` but `.env.agent` is missing the Azure keys. |
| `Bad Request` from Azure with the right key | `AZURE_OPENAI_ENDPOINT` must be the **host only** (e.g. `https://my-resource.openai.azure.com`) — do not include `/openai/v1`. |
| Need Entra ID auth for Azure | Not supported. Use the MAF or openai-agents quickstart. |
| `Energysystems MCP server unreachable at http://localhost:8022/health` | Docker container not running. `docker compose up -d` in `examples/domain_examples/energysystems/`. |
| `Bind for 127.0.0.1:8022 failed: port is already allocated` | A previous container is still holding the port. Find with `docker ps \| grep 8022` and remove with `docker rm -f <name>`. |
| Session created but tool calls never fire | `mcp_servers["energysystems"]["tools"] = []` disables all tools. Use `["*"]` to allow everything. |
| `send_and_wait` times out | Increase the `timeout=...` argument (default 300 s here for the heavy first-run kernel boot). |

## Cleanup

```bash
cd examples/domain_examples/energysystems && docker compose down
```

## Next steps

- **Custom permission handler** — replace
  `PermissionHandler.approve_all` with a function that inspects each
  `PermissionRequest` and approves only the energysystems MCP tools
  (denying anything else the agent might try to add later). See
  [Permission Handling](https://github.com/github/copilot-sdk/blob/main/python/README.md#permission-handling).
- **Streaming** — pass `streaming=True` to `create_session(...)` and
  register an `on_event` handler that filters for
  `assistant.message_delta` events for a typewriter UX.
- **Custom tools** — declare in-process Python functions with
  `@define_tool` and add them to `session_kwargs["tools"]` alongside the
  MCP servers. See
  [Tools](https://github.com/github/copilot-sdk/blob/main/python/README.md#tools).
- **A second MCP server** — append another entry to `mcp_servers` (e.g.
  the chemistry server on `:8020`).
