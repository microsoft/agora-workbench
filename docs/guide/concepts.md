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
| `{name}_parallel_execute`, `{name}_check_batch`, `{name}_collect_batch`, `{name}_cancel_batch` | Fan code out, inspect summaries, collect declared objects, or discard a batch |
| `{name}_send` | Send data to a destination — a peer server's kernel, blob storage, the user (browser download), or the local filesystem |

## Sessions and state

The first `execute_{name}_code` call creates a **session** — a long-lived
kernel. Variables, imports, and loaded data **persist across calls**, so the
agent builds up state incrementally like a notebook rather than re-sending data
every turn. This persistence is the main thing a code-execution server gives you
over stateless tool calls.

Each execution response includes the execution `session_id`. To continue an
active session from another agent or a new MCP connection, pass that value as
`execution_session_id` to `execute_{name}_code`. This is distinct from the MCP
transport session ID; the server resumes only an existing session owned by the
caller, and rejects unknown or expired IDs rather than creating a new kernel.

Parallel batches are an exception: each input runs in a **temporary child
session** reserved for that batch. Child session IDs cannot be used for
additional execution or nested batches, even after their individual jobs
finish. Continue independent work in the parent session or a new session.
Retrieving a terminal status with `{name}_check_batch` closes the children
and, unless exports were declared, retires the batch. Cancellation or closing
the parent also cleans them up. Save an ordinary batch's terminal response,
since subsequent polls cannot retrieve it again.

To keep batch results, assign each child's return value to `result_variable`
(`result` by default). It is captured in `jobs[].result` as JSON when
serializable, otherwise as a `repr` string, but is not automatically injected
into the parent kernel. Files in child `AGORA_OUTPUT_DIR` directories are
deleted during cleanup, so persist needed files to durable storage inside the
batch code and return their references. Object collection is opt-in, as
described below; it does not automatically copy files referenced by objects.

→ See the [Sessions API](../api/sessions.md).

### Collecting batch objects

Declare `export_variable` on `{name}_parallel_execute` to keep a serialized
snapshot of one top-level variable from each successful child. Keep
`result_variable` for small summaries used to choose which objects to collect.
An optional `execution_session_id` selects an existing owned **parent**
execution session; omission uses the current MCP session. Launch always
returns `parent_session_id`, so parent targeting is explicit in the response.

For example, the following illustrates an agent's tool-call sequence:

```python
batch = chemistry_parallel_execute(
    code="candidate = {value, value + 1}\nsummary = {'score': value}",
    inputs=[{"value": 2}, {"value": 5}],
    result_variable="summary",
    export_variable="candidate",
    execution_session_id=parent_id,
)

# Poll until execution is terminal, then choose from collection.staged_indices.
status = chemistry_check_batch(batch_id=batch["batch_id"])
collected = chemistry_collect_batch(
    batch_id=batch["batch_id"],
    import_as="candidates",
    indices=[1],
)
execute_chemistry_code(
    code="print(candidates[1])",
    execution_session_id=collected["execution_session_id"],
)
```

The server snapshots each export before marking its job complete. Execution
and export outcomes are independent: a job can finish successfully but report
`export.status` of `missing`, `failed`, or `too_large`. A real `None` value is
a valid staged object, not a missing export. Child sessions cannot run repair
code, so fix variable-name mistakes or convert unsupported objects to portable
data in the original batch code and relaunch.

For export-enabled batches, terminal polls still close the children, but
**repeated polls do not consume the staged exports**. They report the staged
input indices, `collection.expires_at` (Unix timestamp in seconds), and the
next tool to call. Snapshots expire 15 minutes after all jobs finish, even if
the batch is never polled. Collection is supported only after all jobs finish.

`collect_batch(batch_id, import_as, indices=None, overwrite=False)` loads the
selected snapshots into the recorded parent's kernel as a dictionary with
integer `input_index` keys. Omit `indices` to select all staged exports.
Success reports `imported_indices`, skipped members, and `execution_session_id`
for parent follow-up. Unselected exports are discarded on success. Existing
destination names are rejected unless `overwrite=True`.

A running background job in the parent returns `parent_busy`, its job ID,
and polling guidance. Retry collection after that job finishes; ordinary batch
polls in between are safe. `destination_exists` can be recovered by choosing
another name or explicitly allowing overwrite. `load_failed` identifies an
input index to exclude on retry; `parent_execution_failed` means the kernel
invocation did not produce a collection outcome. Failures do not consume snapshots, but
**do not extend their expiry**. The destination name is assigned only after
all selected loads succeed; deserialization side effects are not rolled back.

Identical successful collection retries return the same receipt without
re-importing. Receipts last at most five minutes and are capped at 128 per
server, so retries are not available forever. A different request cannot
recollect an already consumed batch. `cancel_batch` discards pending snapshots
or a receipt; it does not remove a value already loaded into the parent.

Serialized exports are limited to 256 MiB per object and per batch, with a
shared 1 GiB / 256-object budget and at most 128 pending export batches per
server. Collect or cancel batches to release capacity. Snapshots are private
temporary files outside child output directories, not durable storage.
Parent closure or expiry discards them, and snapshots and receipts are not
recovered after a server restart.

Exports use `dill` snapshots, not shared live identity. Compatible Python
values such as sets and DataFrames can be copied, but live solver handles,
sockets, process or GPU state, and external file contents are not migrated.
Use a portable representation and reconstruct domain state in the parent
when necessary.

## Three layers of guidance for the agent

1. **Tools** — typed `ToolDefinition` functions, found via `search_{name}_tools`.
2. **Skills** — markdown playbooks for multi-step tasks, loaded via
   `load_{name}_skill`. Preferred over manual planning when one fits the task.
3. **State graph** *(optional)* — tools can declare `requires` / `produces`
   states, and `plan_{name}_workflow` turns these into ordered paths. Registered
   only when tools carry state annotations.
4. **Unified state graph** *(router only)* — when a `RouterServer` aggregates
   upstreams with state-annotated tools, it builds a composed `StateGraph` over
   all upstreams and exposes `plan_{router_name}_workflow`. Optionally, `bridges`
   in the router config enable cross-server path queries (e.g., "graphormer
   output → battery simulation") without modifying individual upstream tool
   definitions.

→ See [Skill pattern](skill-pattern.md).

## Working with data

- **Asset provisioning** — large, static files declared in `ServerConfig.assets`
  and fetched into the environment cache at startup.
- **Data catalog** *(optional)* — a searchable index of datasets. When
  configured, the legacy unscoped surface adds `search_data` / `get_artifact` /
  `list_domains` / `query_catalog` (these names are **not** server-prefixed).
  It is appropriate only when the whole catalog is authorized to all callers;
  raw SQL may also be mounted as a separate administrative extension.
- **Asset tags** — the agent embeds references like `<blob>id</blob>` or
  `<local>/path</local>` as string literals; the server resolves them to local
  `Path` objects before the code runs, handling download and auth for you.

→ See [Working with data](working-with-data.md).

## Sidecars: sharing a heavy resource

Because every session runs in its own kernel process, anything a tool loads is
loaded *per session* — fine for a small import, ruinous for a multi-gigabyte
model. A **sidecar** is a long-lived helper process the server launches at
startup so an expensive resource (typically a model) is loaded **once per
container** and shared by all sessions over loopback HTTP. Declare it in
`ServerConfig.sidecars`; the framework starts it, waits for its health check,
exports its URL to every kernel, and stops it on shutdown. A sidecar is a plain
internal service — not an MCP endpoint and invisible to the agent (contrast with
a `ConnectorServer`, which proxies other MCP servers).

→ See [Sidecars](sidecars.md).

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
