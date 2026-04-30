---
description: >
  Weekly audit that cross-references LLM agent prompts (Jinja templates,
  SKILL.md files, domain prompts) against the actual src codebase.
  Detects stale tool references, incorrect parameter signatures, missing
  modules, and other prompt-vs-code drift. Opens an issue assigned to the
  Copilot coding agent when actionable conflicts are found.
on:
  schedule: weekly on wednesday
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
    title-prefix: "[prompt-audit] "
    labels: [prompt-drift]
    assignees: [copilot]
    close-older-issues: true
    max: 1

timeout-minutes: 15
engine: copilot
---

# Prompt ↔ Code Sync Audit

You are an expert code-and-prompt auditor for the **src** project.
Your job is to find conflicts between what the LLM agent prompts _tell the
model to do_ and what the code _actually supports_. When the prompts and the
code disagree, the prompts are "drifted" and must be updated (or, occasionally,
the code must be fixed).

## Repository Context

- **Repository**: ${{ github.repository }}
- **Run ID**: ${{ github.run_id }}
- **Primary source tree**: `src/`

## Prompt Files to Audit

The following file groups contain prompt text that instructs the LLM agent.
Read **every** file in each group.

### Agent system prompts (Jinja2 templates)

```bash
echo "=== Agent system prompts ==="
find src/agent_bot -name '*.jinja' -type f | sort
```

### Domain-specific prompts

```bash
echo "=== Domain prompts ==="
find src/domains -path '*/domain_prompt/*.jinja' -type f | sort
```

### Skill definition files

```bash
echo "=== Skill files ==="
find src/domains -name 'SKILL.md' -type f | sort
```

Read each file's full contents before proceeding to the audit steps.

## Audit Steps

For every prompt file, run each check below. Record **every** mismatch with
the prompt file path, the conflicting line or passage, and the code evidence.

### Check 1 — Tool & Function Name Accuracy

Prompts reference domain tools and utility functions by name (e.g.,
`search_tools`, `run_opf`, `list_tools`, `create_flowsheet`).

1. Extract every tool/function name mentioned in the prompts.
2. Verify each name exists in the codebase. Search in:
   - MCP server tool registrations (`@server.tool`, `@mcp.tool`, tool
     definition dicts)
   - Domain tool modules under `src/domains/*/server/tools/`
   - Core tool files under `src/tools/`
   - Code execution server: `src/code_execution/`

```bash
echo "=== Tool registrations ==="
grep -rn --include='*.py' '@server\.tool\|@mcp\.tool\|"name":' \
  src/domains/*/server/ src/code_execution/ \
  src/tools/ 2>/dev/null | head -80
```

Flag any tool name mentioned in a prompt that **cannot be found** in the code.

### Check 2 — Tool Parameter Signatures

For every tool referenced in a prompt with explicit parameter names or types:

1. Find the tool's actual definition (function signature or schema).
2. Compare required/optional parameters, names, and types.
3. Flag mismatches: missing params, renamed params, wrong types, removed
   params still mentioned in prompts.

```bash
echo "=== Tool function signatures ==="
grep -rn --include='*.py' -A 10 'def run_opf\|def create_flowsheet\|def search_tools\|def load_network\|def solve_flowsheet\|def list_tools' \
  src/ 2>/dev/null | grep -v '.venv' | head -80
```

Extend the grep for any additional tool names you discover in prompts.

### Check 3 — Server & Execution Environment Names

Prompts reference code execution servers by name (e.g.,
`execute_powergrid_code`, `execute_gis_code`, `powergrid_list_sessions`).

1. Extract every `execute_*_code` and `*_list_sessions` pattern from prompts.
2. Verify each server name exists by checking domain server directories and
   MCP server registrations.

```bash
echo "=== Domain server directories ==="
ls -d src/domains/*/server/ 2>/dev/null
echo "=== Code execution tools ==="
grep -rn --include='*.py' 'execute_.*_code\|_list_sessions\|_get_session\|_close_session' \
  src/code_execution/ 2>/dev/null | head -40
```

Flag servers referenced in prompts that don't have a corresponding domain
directory or registration.

### Check 4 — Response Format & Class References

Prompts describe structured response formats (e.g., `AgentResponse`,
`HelpResponse`, `SolutionResponse`, action types, field names).

1. Find the actual response model definitions in code.
2. Compare every field name, action type, and structure mentioned in prompts
   against the code definitions.

```bash
echo "=== Response models ==="
grep -rn --include='*.py' 'class AgentResponse\|class HelpResponse\|class SolutionResponse\|class .*Response.*BaseModel' \
  src/ 2>/dev/null | grep -v '.venv' | head -20
echo "---"
grep -rn --include='*.py' -A 15 'class AgentResponse' \
  src/ 2>/dev/null | grep -v '.venv' | head -40
```

Flag any response class, field, or action type mentioned in prompts that no
longer exists or has been renamed.

### Check 5 — Module & Import Path Validity

Prompts sometimes reference Python modules, packages, or import paths
(e.g., "use PyPSA", "packages available: shapely, geopandas").

1. For each package/module claimed to be available, verify it is listed in
   `src/pyproject.toml` or the relevant domain's
   `requirements.txt`.
2. For internal module paths, verify the files/directories exist.

```bash
echo "=== Top-level dependencies ==="
grep -A 200 '^\[project\]' src/pyproject.toml 2>/dev/null | \
  grep -A 200 'dependencies' | grep -B 0 '^\[' | head -60
echo "=== Domain requirements ==="
for f in src/domains/*/server/requirements.txt; do
  echo "--- $f ---"
  cat "$f" 2>/dev/null
done
```

Flag packages mentioned in prompts that are NOT in the dependency files.

### Check 6 — Data Lake & Asset Tag Conventions

Prompts describe how to use asset tags (e.g., `<blob>...</blob>`) and data
lake access patterns.

1. Find the actual asset tag implementation in code.
2. Verify tag formats, resolution logic, and supported tag types match what
   prompts describe.

```bash
echo "=== Asset tag handling ==="
grep -rn --include='*.py' 'asset.tag\|<blob>\|resolve.*tag\|tag.*blob\|AssetTag\|asset_tag' \
  src/ 2>/dev/null | grep -v '.venv' | head -30
```

### Check 7 — Stale Examples & Code Snippets

Prompts include code examples (Python snippets, tool call demos). For each:

1. Check that function calls in examples use the correct current API.
2. Check that variable names and return value structures match reality.
3. Flag examples that would fail if executed against the current codebase.

### Check 8 — Cross-Prompt Consistency

Compare instructions across different prompt files for contradictions:

1. Do domain prompts contradict the base agent instructions?
2. Do SKILL.md files reference tools or patterns that conflict with their
   parent domain prompt?
3. Are there duplicate or conflicting instructions about the same topic
   (e.g., error handling, response format) across files?

## Report Generation

After all checks, triage findings:

1. **Discard false positives** — e.g., generic names that happen to match,
   comments explaining deprecated patterns, or aspirational references to
   planned features that are clearly marked as such.
2. **Classify remaining findings** by severity:
   - 🔴 **Critical** — prompt instructs the agent to use a tool, function,
     or response format that does not exist; will cause runtime failures.
   - 🟠 **High** — prompt describes wrong parameters, wrong return types,
     or wrong server names; agent will make incorrect calls.
   - 🟡 **Medium** — prompt references packages not in dependencies, uses
     outdated examples, or contains stale file paths.
   - 🔵 **Low** — minor wording inconsistencies, style drift between
     prompts, or informational-only issues.

If **no actionable findings** remain after triage, **do not create an issue** —
simply exit.

## Issue Format

Create a single GitHub issue using `create-issue`. Structure the body as
follows:

```markdown
### Summary

One-paragraph overview: total findings, severity breakdown, and which prompt
files are most affected.

### 🔴 Critical — Runtime Failures

For each finding:
- **Prompt file**: `path/to/prompt.jinja:42` or relevant passage
- **What the prompt says**: quote the conflicting instruction
- **What the code shows**: evidence (file path, function signature, etc.)
- **Suggested fix**: concrete change to the prompt (or code, if applicable)

(Omit section if empty.)

### 🟠 High — Incorrect Instructions

(Same format.)

### 🟡 Medium — Stale References

(Same format.)

### 🔵 Low — Minor Drift

<details>
<summary><b>Expand low-severity findings</b></summary>

(Same format, collapsed for readability.)

</details>

### Recommended Actions

A prioritised checklist the coding agent should follow:
- [ ] Fix critical findings first (prompt or code changes)
- [ ] Correct high-severity parameter/signature mismatches
- [ ] Update stale examples and references
- [ ] Review low-severity items for quick wins
```

## Rules

- Only create an issue when at least one finding survives triage. When
  deciding whether a finding is real, **err on the side of inclusion** — the
  coding agent can dismiss false positives, but it cannot fix problems it
  was never told about.
- Group related findings (e.g., the same stale tool name across multiple
  prompts) into a single item.
- Include enough context for the coding agent to fix each finding without
  re-running the audit.
- Prefer fixing the **prompt** when it has drifted from correct code. Only
  suggest code changes when the code is clearly wrong and the prompt is right.
- Keep the issue concise but thorough.
