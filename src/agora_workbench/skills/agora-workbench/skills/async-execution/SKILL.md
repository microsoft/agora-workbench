---
name: async-execution
description: >
  Run long-running or parallel code asynchronously. Activate when code will
  take more than 30 seconds, when processing many independent inputs, or when
  the user asks to run something in the background.
---

# Asynchronous Execution

## Background Jobs

For code that takes more than ~30 seconds, submit it as a background job:

```
execute_{server}_code(
    code="result = long_running_computation(...)",
    description="Running expensive simulation",
    background=True,
    timeout=3600
)
# Returns immediately with a job_id
```

Then poll for completion:

```
{server}_check_job(job_id="...")
# Returns status: "running", "completed", or "failed"
# When completed, includes stdout, stderr, success, and error (if any)
```

### Rules for background jobs

- The job runs in the same session kernel — variables are accessible after completion.
- Use `{server}_inspect_session` to check both job status and namespace state.
- Set an appropriate `timeout` — background jobs still have a maximum execution time.
- Do not submit trivial code as background — only use for genuinely long-running tasks.

## Parallel Execution

Run the same code template across multiple independent inputs concurrently:

```
{server}_parallel_execute(
    code="result = process_item(input_id=input_id, params=params)",
    inputs=[{"input_id": "item_1", "params": {...}}, {"input_id": "item_2", "params": {...}}, ...],
    result_variable="result"
)
# Returns batch_id
```

### Managing batches

```
{server}_check_batch(batch_id="...")
# Returns aggregate status and available results

{server}_cancel_batch(batch_id="...")
# Stops all running jobs and cleans up child sessions
```

### How parallel execution works

- Each input gets its own child session/kernel.
- The `code` template receives each input dict's keys as local variables.
- The variable named by `result_variable` is collected from each child session.
- Individual items can fail without blocking others.

### When to use parallel execution

- Processing a list of independent inputs with the same logic.
- Tasks where individual items are independent and order does not matter.
- Workloads that benefit from concurrent kernel execution.

### When NOT to use parallel execution

- Tasks that depend on shared state across iterations.
- Small loops (< 5 items) — sequential execution in one `execute_{server}_code` call is simpler and has less overhead.
- Tasks where items must be processed in order.
