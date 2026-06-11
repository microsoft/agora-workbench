# Core concepts

This page explains the mental model behind Agora Workbench — the *what* and
*why*. For what it is in one paragraph, see the [home page](../index.md); for
step-by-step instructions, follow the links to the how-to guides below.

## The core idea: code is the action surface

Most MCP integrations expose each capability as a separate MCP tool with a fixed
JSON schema, and the agent invokes them one structured call at a time. Agora
Workbench takes a different stance for the domain logic itself:

- A server exposes **one primary MCP tool**, `execute_{name}_code`, that runs
  Python in a sandboxed, session-backed kernel.
- **Domain tools are ordinary Python functions injected into that kernel's
  namespace** — they are *not* individual MCP tools. The agent calls them by
  writing code.

So the agent's loop is: *discover what functions exist → write a snippet that
calls and composes them → read the result.* This "code as action" surface fits
coding agents well and has concrete advantages:

- **Composition is free.** Loops, conditionals, intermediate variables, and
  chaining several tools happen in one round-trip instead of N tool calls.
- **Context stays small.** Only one tool schema sits in the agent's context, and
  intermediate data (DataFrames, arrays) stays server-side instead of being
  serialized back and forth through the conversation.
- **Full expressiveness.** The agent has all of Python plus your domain
  packages — not just the parameters a wrapper chose to expose.

The trade-off is that running code requires a sandbox — which is exactly what
the per-server kernel environment provides.

→ See [Tool pattern](tool-pattern.md) and [Writing effective tools and skills](writing-effective-tools.md).

## Two kinds of server

| Server | Role |
|--------|------|
| **`CodeExecutionServer`** | Runs a Python kernel, executes agent code, hosts domain tools and skills, manages sessions. The workhorse. |
| **`ConnectorServer`** | A stateless proxy (router / gateway / dispatcher) that aggregates and routes to several upstream servers — it has no kernel of its own. |

Both subclass `BaseMCPServer` and speak MCP over Streamable HTTP with
bearer-token auth.

→ See [Server options](server-options.md) and [Server networks](server-networks.md).

## What a server actually exposes over MCP

A `CodeExecutionServer` registers a small, fixed set of MCP tools (names are
prefixed with the server name, e.g. `chemistry`):

| MCP tool | Purpose |
|----------|---------|
| `execute_{name}_code` | Run Python in the session kernel — the main action |
| `search_{name}_tools` | Discover domain tools and skills by natural-language query |
| `load_{name}_skill` | Load a skill's markdown instructions |
| `plan_{name}_workflow` | Navigate the state graph (only when tools declare state) |
| `{name}_list_sessions`, `{name}_inspect_session`, `{name}_close_session` | Inspect, list, or close sessions |
| `{name}_parallel_execute`, `{name}_check_batch` | Fan code out across many inputs |
| `{name}_publish_artifact` | Make an output file available to the user |
| `{name}_push_object` | Move a Python object to another server |

## Sessions and state

The first `execute_{name}_code` call creates a **session** — a long-lived
kernel. Variables, imports, and loaded data **persist across calls**, so the
agent builds up state incrementally like a notebook rather than re-sending data
every turn. This persistence is the main thing a code-execution server gives you
over stateless tool calls.

→ See the [Sessions API](../api/sessions.md).

## Three layers of guidance for the agent

1. **Tools** — typed `ToolDefinition` functions, found via `search_{name}_tools`.
2. **Skills** — markdown playbooks for multi-step tasks, loaded via
   `load_{name}_skill`. Preferred over manual planning when one fits the task.
3. **State graph** *(optional)* — tools can declare `requires` / `produces`
   states, and `plan_{name}_workflow` turns these into ordered paths. Registered
   only when tools carry state annotations.

→ See [Skill pattern](skill-pattern.md).

## Working with data

- **Asset provisioning** — large, static files declared in `ServerConfig.assets`
  and fetched into the environment cache at startup.
- **Data catalog** *(optional)* — a searchable index of datasets. When
  configured, it adds the `search_data` / `get_artifact` / `list_domains` /
  `query_catalog` tools (these names are **not** server-prefixed).
- **Asset tags** — the agent embeds references like `<blob>id</blob>` or
  `<local>/path</local>` as string literals; the server resolves them to local
  `Path` objects before the code runs, handling download and auth for you.

→ See [Working with data](working-with-data.md).

## Producing output: artifacts and publishing

Code writes user-facing files into the session output directory — addressed via
the injected `agora_output("name")` helper or the `AGORA_OUTPUT_DIR` variable.
To hand a file to the user, the agent calls `{name}_publish_artifact` with a
destination tag: `<gui>` (browser download, always available), `<blob>`, or
`<local>`.

## Observability: the activity UI

Every server can stream what the agent is doing to the **activity UI** — a
standalone web dashboard (default `http://127.0.0.1:8030`) that shows code
executions, tool searches, skill loads, rendered plots, and published artifacts
in real time, grouped by session. Servers post events fire-and-forget; if the UI
isn't running, code execution is unaffected.

→ See [Monitoring your servers](monitoring.md).

## Authentication

Auth is pluggable. Use **no-op auth** for local development (requests do not require 
a bearer token and any bearer token is accepted) and **Entra ID** for production. 
The server publishes RFC 9728 OAuth metadata so MCP clients can discover how to authenticate.

→ See [Authentication options](authentication.md).

## Where to go next

- [Your First Server](../tutorials/first_server/README.md) — build one end-to-end
- [Options for making a CodeExecutionServer](server-options.md) — every `ServerConfig` knob
- [Attaching to your agent](attaching-to-your-agent.md) — connect a client and inject the workbench skill
