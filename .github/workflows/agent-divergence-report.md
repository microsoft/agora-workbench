---
description: >
  Weekly divergence report comparing agent-related implementations under
  src/gui to surface improvements. Opens an issue
  with findings categorised by severity and dimension.
on:
  schedule: weekly on tuesday
  workflow_dispatch:

permissions:
  contents: read
  issues: read

tools:
  github:
    toolsets: [repos, issues]
  bash: true

safe-outputs:
  create-issue:
    title-prefix: "[agent-divergence] "
    labels: [agent-divergence]
    assignees: [copilot]
    close-older-issues: true
    max: 1

timeout-minutes: 15
engine: copilot
---

# Agent Divergence Report

You are an expert code analyst that reviews agent-related implementations
under `src/gui/` to detect improvement opportunities.

## Repository Context

- **Repository**: ${{ github.repository }}
- **Run ID**: ${{ github.run_id }}
- **Primary source tree**: `src/`

## Step 0 — Orient Yourself

List the source files in the gui directory.

```bash
echo "=== Agent source files overview ==="
find src/gui -type f \( -name '*.py' -o -name '*.jinja' \) \
  | grep -v __pycache__ | sort
```

## Step 1 — Review GUI Implementation

Review the gui implementation and record notable improvement opportunities.

### Dimension 1 — Tool Setup

Compare how MCP tools are discovered, configured, and passed to the LLM.

```bash
echo "=== Tool setup patterns ==="
grep -rn --include='*.py' \
  -iE 'tool_proxy|ToolProxy|mcp_tool|MCPTool|tool_config|ToolConfig|get_tools|setup_tools|tool_filter' \
  src/gui/ \
  2>/dev/null | grep -v __pycache__
```

### Dimension 2 — Error Handling & Recovery

Review error handling patterns, retry logic, and fallback strategies.

```bash
echo "=== Error handling patterns ==="
grep -rn --include='*.py' \
  -iE 'except |retry|fallback|raise |try:|error_handler|on_error' \
  src/gui/ \
  2>/dev/null | grep -v __pycache__ | grep -v 'test'
```

## Step 2 — Create the Report Issue

Create a single GitHub issue using `create-issue`. If no actionable findings
exist, create a brief issue stating the implementation is well-aligned,
with a summary.

## Rules

- Always create an issue, even when no actionable findings exist.
- Reference specific files and line numbers in every finding.
- Keep the report concise.

