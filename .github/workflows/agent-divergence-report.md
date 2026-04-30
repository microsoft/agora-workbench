---
description: >
  Weekly divergence report comparing the four agent implementations under
  src/ (agora, gui, plan_then_execute, toolmaker) to
  surface improvements that could propagate across agents. Opens an issue
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

You are an expert code analyst that compares the four agent implementations
under `src/` to detect innovation propagation
opportunities. The goal is **not** to make the agents identical — some
differences are intentional (GUI needs streaming, PlanThenExecute has
multi-stage workflows, ToolMaker has a four-phase build workflow). The goal is to surface improvements in one agent
that could benefit others.

## Repository Context

- **Repository**: ${{ github.repository }}
- **Run ID**: ${{ github.run_id }}
- **Primary source tree**: `src/`

## Step 0 — Orient Yourself

List the source files in each agent directory so you know what exists. Do
**not** read all files upfront; read individual files on demand when a
specific dimension requires them.

```bash
echo "=== Agent source files overview ==="
find src/agora src/plan_then_execute src/toolmaker src/gui -type f \( -name '*.py' -o -name '*.jinja' \) \
  | grep -v __pycache__ | sort
```

## Step 1 — Compare Across Seven Dimensions

For each dimension below, compare the four agent implementations and record
every notable difference. Categorise each finding by severity:

- 🔴 **High** — An improvement exists in one agent that would likely benefit
  others and is straightforward to adopt.
- 🟡 **Medium** — A difference that may represent a useful improvement but
  needs evaluation before adopting.
- 🔵 **Informational** — An intentional architectural difference; document
  for awareness, no action required.

### Dimension 1 — Response Models

Compare the Pydantic response models in each agent's `response_models.py`.
Look for:

- Models, validators, or field aliases present in one agent but absent from
  others.
- Differences in action types, field names, or validation logic.
- Note: GUI has only help/solution response types (no "continue"), which may
  be intentional — classify as 🔵 Informational unless the absence causes
  problems.

```bash
echo "=== Response models: agora ==="
cat src/agora/response_models.py
echo ""
echo "=== Response models: gui ==="
cat src/gui/response_models.py
echo ""
echo "=== Response models: plan_then_execute ==="
cat src/plan_then_execute/response_models.py
echo ""
echo "=== Response models: toolmaker ==="
cat src/toolmaker/models.py
```

### Dimension 2 — Executor Patterns

Compare `BaseLLMExecutor` implementations, context providers (compaction
strategies, history providers, token budgets), and how tools are wired.

- Which agents define custom executors vs reusing framework defaults?
- Are there differences in how context providers are configured?
- Does one agent handle token budgets or tool wiring more robustly?

```bash
echo "=== Executor: agora ==="
cat src/agora/executors.py
echo ""
echo "=== Executor: plan_then_execute ==="
cat src/plan_then_execute/executors.py
echo ""
echo "=== Executor: toolmaker ==="
cat src/toolmaker/executors.py
echo ""
echo "=== Agent entry points ==="
echo "--- agora/agent.py ---"
cat src/agora/agent.py
echo ""
echo "--- gui/agent.py ---"
cat src/gui/agent.py
echo ""
echo "--- plan_then_execute/agent.py ---"
cat src/plan_then_execute/agent.py
echo ""
echo "--- toolmaker/agent.py ---"
cat src/toolmaker/agent.py
```

### Dimension 3 — Skill Integration

Check whether each agent uses `SkillsProvider`, `SkillAwareToolCompactionStrategy`,
or equivalent skill-aware patterns. Note which agents reference skills in
prompts vs code.

```bash
echo "=== Skill references across agents ==="
grep -rn --include='*.py' --include='*.jinja' \
  -iE 'skill|SkillsProvider|SkillAware' \
  src/agora/ \
  src/gui/ \
  src/plan_then_execute/ \
  src/toolmaker/ \
  2>/dev/null | grep -v __pycache__
```

### Dimension 4 — Tool Setup

Compare how MCP tools are discovered, configured, and passed to the LLM.
Look for differences in tool filtering, tool configuration, or tool proxy
patterns.

```bash
echo "=== Tool setup patterns ==="
grep -rn --include='*.py' \
  -iE 'tool_proxy|ToolProxy|mcp_tool|MCPTool|tool_config|ToolConfig|get_tools|setup_tools|tool_filter' \
  src/agora/ \
  src/gui/ \
  src/plan_then_execute/ \
  src/toolmaker/ \
  2>/dev/null | grep -v __pycache__
```

### Dimension 5 — Prompt Differences

Compare the Jinja2 templates for structural differences. Focus on instruction
categories (safety, error handling, response format, tool usage) present in
one agent but absent from others.

```bash
echo "=== Prompt templates ==="
for f in $(find src/agora src/plan_then_execute src/toolmaker src/gui -name '*.jinja' -type f | sort); do
  echo ""
  echo "========== $f =========="
  cat "$f"
done
```

### Dimension 6 — Context Compaction

Compare compaction strategies used by each agent. Look for:

- `SlidingWindowStrategy`, `SummarizationStrategy`,
  `TokenBudgetComposedStrategy`, `ToolResultCompactionStrategy`
- Which agents use skill-aware compaction?
- Are there differences in token budget allocation?

```bash
echo "=== Compaction strategy references ==="
grep -rn --include='*.py' \
  -iE 'compaction|SlidingWindow|Summarization|TokenBudget|ToolResult.*Strategy' \
  src/agora/ \
  src/gui/ \
  src/plan_then_execute/ \
  src/toolmaker/ \
  2>/dev/null | grep -v __pycache__
```

### Dimension 7 — Error Handling & Recovery

Compare error handling patterns, retry logic, and fallback strategies across
agents.

```bash
echo "=== Error handling patterns ==="
grep -rn --include='*.py' \
  -iE 'except |retry|fallback|raise |try:|error_handler|on_error' \
  src/agora/ \
  src/gui/ \
  src/plan_then_execute/ \
  src/toolmaker/ \
  2>/dev/null | grep -v __pycache__ | grep -v 'test'
```

## Step 2 — Triage Findings

After comparing all dimensions:

1. **Discard false positives** — differences that are clearly explained by
   the agent's purpose (e.g., GUI's streaming response model, PlanThenExecute's
   multi-stage plan structure).
2. **Mark intentional divergence** as 🔵 Informational with a brief
   explanation of why the difference exists.
3. **Identify propagation opportunities** — improvements in one agent that
   others lack. Mark as 🔴 High (straightforward to adopt) or 🟡 Medium
   (needs evaluation).

## Step 3 — Create the Report Issue

Create a single GitHub issue using `create-issue`. If no actionable findings
(🔴 or 🟡) exist, create a brief issue stating all agents are well-aligned,
with a summary of intentional differences.

Structure the issue body as follows:

```markdown
### Summary

One-paragraph overview: total findings by severity (🔴 N high, 🟡 N medium,
🔵 N informational), which dimensions had the most divergence, and which
agent has the most propagation opportunities.

### 🔴 High — Propagation Opportunities

For each finding:
- **Dimension**: which comparison dimension
- **Source agent**: which agent has the improvement
- **What it does**: describe the improvement
- **Missing from**: which agent(s) lack it
- **Files**: `path/to/source.py:NN` (specific file and line references)
- **Suggested action**: concrete next step to propagate

(Omit section if empty.)

### 🟡 Medium — Potential Improvements

For each finding:
- **Dimension**: which comparison dimension
- **Difference**: describe what differs
- **Agents**: which agents are involved
- **Files**: specific file references
- **Evaluation needed**: what must be assessed before adopting

(Omit section if empty.)

### 🔵 Informational — Intentional Divergence

<details>
<summary><b>Expand intentional divergence details</b></summary>

For each finding:
- **Dimension**: which comparison dimension
- **Difference**: describe the divergence
- **Rationale**: why this difference is intentional
- **Agents**: which agents are involved

</details>

### Recommendations

A prioritised checklist of concrete next steps:
- [ ] Propagate high-severity improvements first
- [ ] Evaluate medium-severity differences
- [ ] Document intentional divergence in code comments if not already documented
```

## Rules

- Always create an issue, even when no actionable findings exist — the weekly
  cadence provides a useful health signal.
- If context limitations prevent completing the full analysis, call `noop`
  with a message such as "Analysis incomplete due to context limits — rerun
  or scope manually." Never exit silently without calling at least one safe
  output tool.
- Reference specific files and line numbers in every finding.
- Group related findings within the same dimension.
- Distinguish clearly between "missed improvement" (actionable) and
  "intentional divergence" (informational).
- Keep the report concise. Use `<details>` tags for verbose diffs or long
  code excerpts.
- Do not suggest making the agents identical. Respect each agent's
  architectural purpose.
