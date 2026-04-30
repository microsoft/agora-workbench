# Plan Organization

Use this skill to keep large or complex plans navigable. Tags group steps into
logical categories. Queries let you focus on a specific subset of steps.
History provides an audit trail of every change made to the plan.

## When to Use

- A plan has more than ~10 steps and grouping by phase or category would help
- You want to find all steps of a particular kind (e.g. all `validation` steps)
- You need a count of steps by status without listing every step
- You want to audit what changed in the plan and when

## Core Tool Reference

| Tool | Purpose | Required parameters |
|------|---------|-------------------|
| `tag_step` | Attach a label to a step | `step_id`, `tag` |
| `untag_step` | Remove a label from a step | `step_id`, `tag` |
| `query_steps` | Filter steps by status, tag, and/or readiness | `status?`, `tag?`, `ready_only?` |
| `plan_summary` | Get step counts grouped by status | *(none)* |
| `get_history` | Retrieve the change log (plan-wide or per step) | `step_id?` |

## Tags

Tags are free-form text labels attached to individual steps. A step can have
multiple tags; a tag can be applied to multiple steps.

### Common Tag Conventions

| Tag | Purpose |
|-----|---------|
| `data-prep` | Data loading, cleaning, and validation steps |
| `computation` | Analysis, modelling, or simulation steps |
| `validation` | Correctness checks and sanity tests |
| `reporting` | Output, export, and presentation steps |
| `phase-1`, `phase-2`, … | Multi-phase workflows where steps belong to distinct phases |

### Applying Tags

```
tag_step(step_id=1, tag="data-prep")
tag_step(step_id=2, tag="data-prep")
tag_step(step_id=3, tag="computation")
tag_step(step_id=4, tag="computation")
tag_step(step_id=5, tag="validation")
tag_step(step_id=6, tag="reporting")
```

### Removing a Tag

```
untag_step(step_id=3, tag="computation")
```

## Querying Steps

`query_steps` returns a JSON array of step objects. Use it to focus on a
specific slice of the plan without viewing the whole thing.

### By status

```
query_steps(status="failed")
```

Returns all failed steps — useful for triaging problems.

### By tag

```
query_steps(tag="validation")
```

Returns all steps tagged `validation`, regardless of their status.

### By status and tag

```
query_steps(status="pending", tag="data-prep")
```

Returns pending data-preparation steps only.

### Ready to execute (no unfinished prerequisites)

```
query_steps(status="pending", ready_only=True)
```

Returns pending steps whose dependency prerequisites are all completed.
See the `plan-dependencies` skill for how to set up dependencies.

## Plan Summary

`plan_summary` gives a quick overview without listing individual steps:

```
plan_summary()
```

Example response:

```json
{
  "pending": 5,
  "in_progress": 1,
  "completed": 8,
  "failed": 1,
  "skipped": 0,
  "total": 15
}
```

Use this to gauge overall progress or to decide whether to report to the user
before continuing.

## History and Auditing

`get_history` returns an append-only log of every change made to the plan.

### Full plan history

```
get_history()
```

### History for a single step

```
get_history(step_id=3)
```

Each history record contains:

```json
{
  "history_id": 12,
  "plan_id": "...",
  "step_id": 3,
  "action": "set_step_status",
  "data": "{\"status\": \"completed\", \"notes\": \"Converged in 3 iterations\"}",
  "timestamp": "2025-06-01T14:32:11+00:00"
}
```

Common `action` values:

| Action | When recorded |
|--------|-------------|
| `add_step` | A step was appended |
| `insert_step` | A step was inserted at a specific position |
| `update_step` | A step's description or notes were changed |
| `set_step_status` | A step's status was changed |
| `remove_step` | A step was deleted |
| `add_dependency` | A dependency edge was added |
| `remove_dependency` | A dependency edge was removed |
| `tag_step` | A tag was attached |
| `untag_step` | A tag was removed |
| `finalize` | The plan was finalized |

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| Using query_steps instead of view_plan during active execution | Both are valid; prefer `query_steps(status="in_progress")` to see exactly what is running |
| Creating many one-off tags that are never reused | Agree on a small tag vocabulary at planning time and stick to it |
| Forgetting to call `plan_summary` when giving the user a status update | `plan_summary` provides a compact, unambiguous count that complements the prose update |
