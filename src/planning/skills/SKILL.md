---
name: planning
description: Build, track, and organize multi-step execution plans using the planning tools (add_step, set_step_status, add_dependency, tag_step, query_steps, etc.).
---

# Planning

Use this skill when you have planning tools available and the user's task
requires a structured, multi-step execution plan. The planning package provides
SQLite-backed persistence, dependency tracking, tagging, and an audit log.

## Quick Reference

| Phase | Key tools | When |
|-------|-----------|------|
| **Building** | `add_step`, `insert_step`, `update_step`, `remove_step`, `finalize_plan`, `view_plan` | Before execution — draft and revise the plan |
| **Execution tracking** | `set_step_status`, `update_step_notes`, `plan_summary`, `query_steps` | During execution — keep statuses accurate |
| **Dependencies** | `add_dependency`, `remove_dependency`, `query_steps(ready_only=True)` | When step ordering matters beyond list order |
| **Organization** | `tag_step`, `untag_step`, `query_steps(tag=...)`, `get_history` | For large plans — group, filter, and audit |

## Typical Workflow

1. **Plan** — call `add_step` for each logical step, review with `view_plan`,
   revise with `insert_step` / `update_step` / `remove_step`, then
   `finalize_plan` once the user approves.

2. **Execute** — for each step: `set_step_status("in_progress")`, do the work,
   then `set_step_status("completed", notes="...")`. Use
   `query_steps(status="pending", ready_only=True)` to pick the next step.

3. **Report** — call `plan_summary` for a status overview, or `get_history` for
   a full audit trail.

## Detailed Guides

For in-depth guidance on each aspect of planning, see:

- [Plan Building](references/plan-building.md) — drafting, revising, and finalizing plans
- [Plan Execution Tracking](references/plan-execution-tracking.md) — status lifecycle and progress tracking
- [Plan Dependencies](references/plan-dependencies.md) — prerequisite edges, ready-step queries, fan-out/fan-in
- [Plan Organization](references/plan-organization.md) — tags, filtered queries, summary, and audit history
- [Step Writing Guide](references/step-writing-guide.md) — how to write clear, actionable step descriptions
