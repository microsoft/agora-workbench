# Plan Execution Tracking

Use this skill during the **execution phase** to keep the plan's status
accurate as you work through each step. Accurate status tracking lets the user
follow along and ensures the presentation phase has a faithful record of what
was done.

## When to Use

- A plan has been finalized and execution has begun
- You are about to start, finish, or skip a step
- You want to surface which steps remain, are in-progress, or have failed
- You need to add notes about a step's outcome or failure reason

## Status Lifecycle

Each step follows this lifecycle:

```
pending  →  in_progress  →  completed
                         →  failed
                         →  skipped
```

| Status | Meaning |
|--------|---------|
| `pending` | Not yet started (initial state) |
| `in_progress` | Currently being worked on |
| `completed` | Successfully finished |
| `failed` | Attempted but could not finish — always add notes explaining why |
| `skipped` | Deliberately bypassed — always add a note explaining why |

## Core Tool Reference

| Tool | Purpose | Required parameters |
|------|---------|-------------------|
| `set_step_status` | Change a step's status | `step_id`, `status`, `notes?` |
| `update_step_notes` | Add or revise a step's notes | `step_id`, `notes` |
| `view_plan` | See all steps with current statuses | *(none)* |
| `plan_summary` | Get counts by status (overview) | *(none)* |
| `query_steps` | Filter steps by status or tag | `status?`, `tag?`, `ready_only?` |

## Execution Pattern

Before starting each step:

```
set_step_status(step_id=<N>, status="in_progress")
```

After completing:

```
set_step_status(step_id=<N>, status="completed", notes="<brief outcome summary>")
```

On failure:

```
set_step_status(step_id=<N>, status="failed", notes="<what went wrong and why>")
```

Only mark a step `failed` if you genuinely could not complete it. If you chose
to skip it (e.g. because a prerequisite produced no data), use `skipped`.

## Checking Remaining Work

After completing a step, see what is still pending:

```
query_steps(status="pending")
```

To see only steps that are ready to start (no unfinished prerequisites):

```
query_steps(status="pending", ready_only=True)
```

For a high-level overview without the full step list:

```
plan_summary()
```

Example `plan_summary` response:

```json
{
  "pending": 3,
  "in_progress": 1,
  "completed": 4,
  "failed": 0,
  "skipped": 1,
  "total": 9
}
```

## Notes Best Practices

Always add `notes` when setting `failed` or `skipped`. Notes on `completed`
steps are optional but useful for steps that produce important intermediate
results:

```
set_step_status(
    step_id=4,
    status="completed",
    notes="Found 7 overloaded lines; worst is line L-42 at 118% loading"
)
```

```
set_step_status(
    step_id=6,
    status="skipped",
    notes="Step 5 produced no overloaded lines, so remedial action is not needed"
)
```

```
set_step_status(
    step_id=3,
    status="failed",
    notes="Power flow did not converge — isolated sub-network detected at bus B-17"
)
```

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| Forgetting to set `in_progress` before starting | Call `set_step_status(status="in_progress")` first so the user knows work is underway |
| Marking a step `completed` without notes when the output matters | Add a brief outcome note to aid the presentation phase |
| Marking a step `failed` without explaining why | Always include `notes` explaining the failure |
| Leaving steps `in_progress` if you pivot to a different step | Update the previous step to `failed` or `pending` before switching |
| Executing steps out of order when dependencies exist | Use `query_steps(ready_only=True)` to see which steps are unblocked |

## Example: End-of-Step Sequence

```
# Starting step 3
set_step_status(step_id=3, status="in_progress")

# ... execute domain tool calls ...

# Step complete
set_step_status(step_id=3, status="completed", notes="Baseline power flow converged; max line loading 74%")

# What's next?
query_steps(status="pending", ready_only=True)
```
