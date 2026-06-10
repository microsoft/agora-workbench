---
name: agora-workbench
description: >
  Use Agora Workbench MCP servers for domain-specific Python code execution,
  tool discovery, workflow planning, and data management. Activate when
  connected to any MCP server built with agora-workbench (CodeExecutionServer
  or ConnectorServer), or when the user asks about domain tools, code
  execution environments, or multi-server workflows.
compatibility: Requires an MCP-compatible agent client with Streamable HTTP transport support.
metadata:
  author: microsoft
  version: "1.0"
---

# Agora Workbench

## Core Mental Model

- Domain tools are **Python functions injected into the kernel namespace** — they are NOT individual MCP tools. Call them by writing Python code inside `execute_{server}_code`.
- Sessions are **persistent** — variables, imports, and state survive across `execute_{server}_code` calls. Do not recompute or re-import unnecessarily.
- Always **discover before using** — call `search_{server}_tools` before assuming a domain function exists. Never guess tool names.

## Discovery

Before writing any domain code, discover what is available:

```
search_{server}_tools(query="", top=999)           # Full catalog
search_{server}_tools(query="molecular weight")    # Targeted search
search_{server}_tools(query="screening", category="skills")  # Skills only
```

- Results are grouped into `tools` (callable functions) and `skills` (multi-step workflows).
- If a skill matches the task, load it with `load_{server}_skill(skill_name="...")` and follow its instructions.
- If the task is simple, call tools directly.

## Executing Code

The primary MCP tool is `execute_{server}_code`:

| Parameter | Usage |
|-----------|-------|
| `code` | Python code that calls domain functions from the kernel namespace |
| `description` | One-sentence summary shown to the user — **always set this** |
| `timeout` | Seconds before execution is killed (increase for heavy computation) |
| `background` | Set `True` for long-running jobs; poll with `{server}_check_job(job_id=...)` |

### Canonical example

```
# 1. Discover
search_{server}_tools(query="data processing")

# 2. Execute
execute_{server}_code(
    description="Process input data and extract results",
    code="result = process_data(input_id='item_001')\nprint(result)",
    timeout=60
)
```

### Error handling

- If execution fails, read `stderr` and `error` from the result.
- Fix the code and retry in the same session — prior state is still available.
- If a tool raises `ValueError`, it means invalid input. Read the message and adjust arguments.

## Sessions and State

- A session is created automatically on first `execute_{server}_code` call.
- All subsequent calls reuse the same session (variables persist).
- Use `{server}_inspect_session(session_id=...)` to see what variables exist and check background job status.
- Use `{server}_close_session(session_id=...)` only when done with a domain entirely.
- Use `{server}_list_sessions()` to see active sessions.

## Artifacts and Publishing

See the [artifacts sub-skill](skills/artifacts/SKILL.md) for data fetching, cross-server transfer, and destination tag details.

## Workflow Planning

For complex multi-step tasks with ordered dependencies, use the state graph:

```
plan_{server}_workflow(mode="overview")    # See the full graph
plan_{server}_workflow(mode="path", current_state="...", target_state="...")  # Get a sequence
```

See the [workflow-planning sub-skill](skills/workflow-planning/SKILL.md) for full mode details and skill loading patterns.

## Parallel Tool Execution

See the [async-execution sub-skill](skills/async-execution/SKILL.md) for submitting background jobs for long-running code and parallel execution across multiple inputs.

## Handling Large Objects

Stdout from `execute_{server}_code` is returned in the MCP tool response and
consumed as agent context tokens. Overflowing this with large objects wastes
context, triggers server-side truncation, and can degrade agent reasoning.

### Rules

1. **Never print large objects verbatim** — no `print(df)`, `print(long_list)`,
   or `print(json.dumps(big_dict))`.
2. **Summarize instead** — use `.head()`, `.shape`, `len()`, `.describe()`,
   `.columns.tolist()`, or slicing to extract only what you need.
3. **Store results in variables** — keep data in the persistent session and
   inspect it with targeted follow-up calls:
   ```python
   # ✓ Good — store and summarize
   result = compute_expensive_thing(...)
   print(f"Shape: {result.shape}, columns: {result.columns.tolist()}")
   print(result.head(5).to_string())

   # ✗ Bad — dumps entire object into MCP response
   result = compute_expensive_thing(...)
   print(result)
   ```
4. **Write large outputs to files** — use `AGORA_OUTPUT_DIR` for data intended
   for the user; use `/tmp` for intermediate scratch files you'll read back
   server-side.
5. **Use `{server}_send`** for cross-server transfers — never serialize
   large objects through stdout to paste into another server call.
6. **Paginate when exploring** — if you need to see rows 50–100 of a DataFrame,
   slice it: `print(df.iloc[50:100].to_string())`.

### What happens if you exceed the limit

The server truncates stdout/stderr that exceeds its configured threshold
(default 50 KB). The truncated response includes a notice. While this prevents
context overflow, the lost information may require re-execution — so avoid
hitting the limit proactively.

## Do Not

- Do not call domain functions as MCP tools — they only exist inside the kernel.
- Do not guess tool names — always search first.
- Do not write output files outside `AGORA_OUTPUT_DIR`.
- Do not paste large data into chat when `{server}_send` can transfer it server-to-server.
- Do not print entire DataFrames, large lists, or raw API responses — summarize or slice.
- Do not run long jobs synchronously — use `background=True` and poll.
- Do not create new sessions unnecessarily — reuse the existing one.
- Do not publish artifacts unless the user explicitly asks for a download or export.
