---
name: workflow-planning
description: >
  Plan and navigate multi-step domain workflows using the state graph. Activate
  when a task involves ordered tool dependencies, multiple state transitions,
  or when unsure which tool to call next in a domain sequence.
---

# Workflow Planning

## When to Use

Use workflow planning when:

- The task spans multiple state transitions (e.g., parse → compute → filter → cluster).
- You are unsure which tool comes next or what prerequisites exist.
- You want to see all possible paths through a domain's tool graph.

Do NOT use for simple single-tool tasks — just search and call directly.

**Not all servers expose workflow planning tools.** They are only registered when
the server has tools with state annotations (`requires`/`produces`). If
`plan_{server}_workflow` is not listed in the server's available tools, skip
planning and use `search_{server}_tools` to find what you need directly.

## The State Graph

Domain tools declare state transitions:

- **`requires`** — states that must exist before this tool can run.
- **`produces`** — states this tool generates after successful execution.

These form a directed graph that `plan_{server}_workflow` can navigate.

## Available Modes

### Overview — see the full graph

```
plan_{server}_workflow(mode="overview")
```

Returns all tools with their state requirements and productions. Use this first
to understand the domain's structure.

### Path — get a recommended sequence

```
plan_{server}_workflow(
    mode="path",
    current_state="{domain}.data_loaded",
    target_state="{domain}.results_exported"
)
```

Returns an ordered list of tools to call to reach the target state from the
current state. Follow this sequence in your `execute_{server}_code` calls.

### Tool — inspect a specific tool's context

```
plan_{server}_workflow(mode="tool", tool_name="process_data")
```

Returns the tool's prerequisites, what it produces, and where it fits in the graph.

## Workflow with Skills

For common multi-step workflows, load a pre-built skill instead of planning manually:

1. `search_{server}_tools(query="...", category="skills")` — find relevant skills.
2. `load_{server}_skill(skill_name="...")` — load the skill's step-by-step instructions.
3. Follow the loaded instructions, which reference specific tools in the correct order.

Skills are preferred over manual planning when they exist for your task — they
encode domain expertise and edge-case handling that raw graph traversal does not.
