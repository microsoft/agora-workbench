# Plan Building

Use this skill during the **planning phase** to build a clear, ordered execution
plan before any domain actions are taken. The agent should never start executing
domain tasks until the plan is finalized.

## When to Use

- The user has described a task that requires multiple steps or stages
- You need to clarify scope, ordering, or approach before acting
- You are revising a plan in response to user feedback

## Core Tool Reference

| Tool | Purpose | Required parameters |
|------|---------|-------------------|
| `view_plan` | Show the current plan with all step statuses | *(none)* |
| `add_step` | Append a step to the end of the plan | `description` |
| `insert_step` | Insert a step after a specific step_id | `after_step_id`, `description` |
| `update_step` | Change a step's description or notes | `step_id`, `description?`, `notes?` |
| `remove_step` | Delete a step from the plan | `step_id` |
| `finalize_plan` | Lock the plan and begin execution | *(none)* |

## Typical Workflow

### 1 — Draft the plan

Call `add_step` once per logical step in the order they should run:

```
add_step("Load and inspect the transmission network dataset")
add_step("Run DC power flow to establish a baseline")
add_step("Identify overloaded lines (loading > 90%)")
add_step("Propose remedial actions and re-run to verify improvement")
add_step("Summarize findings and generate a report")
```

Then call `view_plan` to review the result before presenting it to the user.

### 2 — Revise in response to feedback

- **Add a missing step** anywhere with `insert_step`:

  ```
  insert_step(after_step_id=2, description="Check for isolated network components before running power flow")
  ```

  Use `after_step_id=0` to insert before all existing steps.

- **Rename or annotate a step** with `update_step`:

  ```
  update_step(step_id=3, description="Identify overloaded lines (loading > 80%) and rank by severity")
  ```

- **Remove a step** that is no longer needed:

  ```
  remove_step(step_id=5)
  ```

- Call `view_plan` after every structural change to verify the plan looks correct.

### 3 — Finalize

Once the user approves the plan, call `finalize_plan`. This transitions the
workflow to the execution phase.

```
finalize_plan()
```

**Do not finalize an empty plan.** Add at least one step first.

## Writing Good Step Descriptions

See [step-writing-guide.md](step-writing-guide.md) for
detailed guidance. The key rules are:

- Start with a **verb** that describes the action: *Load*, *Run*, *Calculate*, *Verify*, *Report*.
- Be **specific** about inputs and outputs: "Run AC power flow on the IEEE 118-bus network" beats "Run power flow".
- Keep each step to a **single responsibility**. Split compound steps with "and" into two separate steps.
- Avoid vague language: *Check things*, *Analyze the data*, *Do the analysis* — these give the executor no guidance.

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| Starting execution before calling `finalize_plan` | Always finalize before leaving the planning phase |
| Forgetting to call `view_plan` after edits | Review the plan after every structural change |
| Writing steps that are too coarse ("Analyze everything") | Break into concrete, single-responsibility steps |
| Not numbering steps logically | Use `insert_step` to add steps in the right position rather than appending them out of order |
| Planning steps that depend on each other but are ordered incorrectly | Re-order steps or use `add_dependency` to encode prerequisites explicitly |
