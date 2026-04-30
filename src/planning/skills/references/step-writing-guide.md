# Step Writing Guide

Well-written steps make the execution phase predictable and auditable.

## The One-Step, One-Action Rule

Each step should represent exactly one discrete action. If a step description
contains "and" connecting two distinct actions, split it.

| ✗ Too broad | ✓ Better |
|-------------|----------|
| Load data and run analysis | Step 1: Load data; Step 2: Run analysis |
| Calculate results and generate report | Step 1: Calculate results; Step 2: Generate report |

## Verb-First Descriptions

Start every step description with a strong, unambiguous action verb:

| Category | Good verbs |
|----------|-----------|
| Data access | Load, Fetch, Download, Read, Query |
| Computation | Run, Compute, Calculate, Solve, Fit |
| Validation | Verify, Check, Confirm, Assert |
| Transformation | Convert, Normalize, Filter, Aggregate |
| Output | Generate, Export, Summarize, Report, Plot |

## Specificity Checklist

A well-written step answers:

1. **What** is the action? (the verb)
2. **What** is the target object? (dataset, model, file, parameter)
3. **What** is the desired output or success criterion? (optional but helpful)

### Examples

| Step | Quality | Notes |
|------|---------|-------|
| "Do analysis" | ✗ Poor | No action verb; no target; no criterion |
| "Analyze the data" | ✗ Poor | Vague target; no criterion |
| "Run the model" | ✗ Poor | Which model? |
| "Calculate line loading ratios for each transmission line" | ✓ Good | Specific target and output |
| "Verify convergence of the power flow solution (all buses within ±5% voltage)" | ✓ Good | Clear criterion |
| "Export the results table to CSV and attach to the data lake" | ✓ Good | Specific action chain (acceptable two-part step since both are output actions) |

## Notes Field

Use the `notes` field (via `update_step`) to record implementation context that
should not clutter the description:

- Specific tool names to call
- Data lake asset IDs
- Threshold values that may change
- Known pitfalls for this particular step

```
update_step(step_id=3, notes="Use query_steps(status='pending', ready_only=True) to see which steps are unblocked")
```

## Granularity Guidelines

| Task complexity | Recommended step count |
|----------------|----------------------|
| Simple one-domain task | 3–6 steps |
| Multi-stage analysis | 6–12 steps |
| Complex multi-domain workflow | 12–20 steps |
| More than ~20 steps | Consider grouping into phases using tags |

When a workflow exceeds ~20 steps, group related steps with a shared tag
(e.g. `tag_step(step_id, "phase-1-data-prep")`) so the agent and user can
reason about phases as a whole rather than individual steps.
