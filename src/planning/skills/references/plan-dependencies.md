# Plan Dependencies

Use this skill when the execution order of steps matters — particularly when
one step produces data or a result that a later step must consume. Encoding
these relationships prevents the agent from running steps in the wrong order
and surfaces which steps are ready to execute at any moment.

## When to Use

- A step cannot start until one or more earlier steps are **completed**
- You want the plan to enforce sequencing constraints beyond simple top-to-bottom order
- The plan branches into parallel paths that converge later
- You want to query only the steps that are currently unblocked

## Core Tool Reference

| Tool | Purpose | Required parameters |
|------|---------|-------------------|
| `add_dependency` | Block `step_id` until `depends_on` is completed | `step_id`, `depends_on` |
| `remove_dependency` | Remove a prerequisite edge | `step_id`, `depends_on` |
| `query_steps` | Find steps that are unblocked and ready to start | `status="pending"`, `ready_only=True` |

## Dependency Semantics

```
add_dependency(step_id=B, depends_on=A)
```

Means: **Step B cannot start until Step A is completed.**

If step A has status `pending`, `in_progress`, `failed`, or `skipped`, step B
is **blocked**. Step B becomes **ready** only when step A reaches `completed`.

> **Note**: Dependencies track `completed` specifically. A `skipped` or
> `failed` prerequisite does **not** unblock dependent steps. If you skip
> a prerequisite intentionally and still want to run the dependent step,
> remove the dependency first with `remove_dependency`.

## Typical Pattern

```
# 1. Build the plan
add_step("Load the network dataset")               # id: 1
add_step("Validate network topology")              # id: 2
add_step("Run baseline power flow")                # id: 3
add_step("Identify overloaded lines")              # id: 4
add_step("Propose and apply remedial actions")     # id: 5
add_step("Re-run power flow after remediation")    # id: 6
add_step("Generate final report")                  # id: 7

# 2. Encode dependencies
add_dependency(step_id=2, depends_on=1)   # validate only after loading
add_dependency(step_id=3, depends_on=2)   # run power flow after validation
add_dependency(step_id=4, depends_on=3)   # identify overloads after baseline
add_dependency(step_id=5, depends_on=4)   # remedial actions after identification
add_dependency(step_id=6, depends_on=5)   # re-run after applying remediation
add_dependency(step_id=7, depends_on=6)   # report at the very end
```

## Querying Ready Steps

During execution, always check which steps are unblocked before choosing what to run next:

```
query_steps(status="pending", ready_only=True)
```

Returns only steps whose **all** dependencies are completed. Steps that are
blocked by an unfinished prerequisite are excluded.

## Parallel Steps (Fan-Out / Fan-In)

When two steps can run concurrently, give them the same prerequisite but do
**not** make them depend on each other:

```
# After step 1 completes, steps 2 and 3 can run in parallel
add_dependency(step_id=2, depends_on=1)
add_dependency(step_id=3, depends_on=1)
# Step 4 can only run after both 2 and 3 are done
add_dependency(step_id=4, depends_on=2)
add_dependency(step_id=4, depends_on=3)
```

## Removing a Dependency

Use `remove_dependency` when circumstances change and a prerequisite is no
longer required:

```
remove_dependency(step_id=3, depends_on=2)
```

## Cycle Prevention

The planning package automatically rejects dependency edges that would
create a cycle (e.g. step A depends on B, and B depends on A). If you see
an error like `"Adding dependency ... would create a cycle"`, check the
existing dependency graph and correct the ordering.

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| Running a step before its prerequisite is `completed` | Use `query_steps(ready_only=True)` to get the unblocked set |
| Adding a dependency in the wrong direction | `add_dependency(step_id=B, depends_on=A)` means "B depends on A" — double-check the argument order |
| Expecting `skipped` to unblock dependents | `skipped` does not count as `completed`; remove the dependency if you skip a prerequisite intentionally |
| Accidentally creating a chain that can never finish | Ensure at least one step has no dependencies — it will be the first to become ready |
