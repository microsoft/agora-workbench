---
description: >
  Weekly audit that cross-references other agentic workflow source files
  (.md) against the current state of the repository. Detects stale code
  snippets, outdated repo paths, incorrect GitHub Actions expressions,
  drifted architecture descriptions, and other prompt-vs-reality conflicts.
  Opens an issue assigned to the Copilot coding agent when actionable
  findings are found.
on:
  schedule: weekly on thursday
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
    title-prefix: "[workflow-audit] "
    labels: [workflow-drift]
    assignees: [copilot]
    close-older-issues: true
    max: 1

timeout-minutes: 15
engine: copilot
---

# Agentic Workflow Self-Audit

You are an expert auditor responsible for keeping the repository's **agentic
workflow source files** (`.github/workflows/*.md`) accurate and up to date.

Agentic workflows contain natural-language instructions, embedded shell
commands, file path references, architecture descriptions, and GitHub Actions
expressions. All of these can drift as the codebase evolves. Your job is to
detect that drift and file an issue so the Copilot coding agent can fix it.

## Repository Context

- **Repository**: ${{ github.repository }}
- **Run ID**: ${{ github.run_id }}

## Step 0 — Discover Workflow Sources

List all agentic workflow source files. **Do not audit this workflow file** —
skip `workflow-self-audit.md` when listing files.

```bash
echo "=== Agentic workflow source files ==="
find .github/workflows -maxdepth 1 -name '*.md' -type f \
  | grep -v 'workflow-self-audit.md' \
  | sort
```

Read the **full contents** of every listed file before proceeding to audits.

## Audit Checks

For each workflow source file, run every check below. Record each finding
with the workflow filename, the relevant line or passage, and the evidence.

### Check 1 — Embedded Shell Snippets

Many workflows contain fenced `bash` code blocks with `grep`, `find`, or
other commands that reference specific file paths, patterns, or directory
structures.

For each shell snippet:

1. Extract every file path and glob pattern from the snippet.
2. Verify those paths/patterns still match files in the repo.
3. Run the snippet (or a safe subset) and compare the output to what the
   workflow author clearly expects. Flag snippets that produce no output,
   error, or clearly wrong results.

```bash
echo "=== Repo top-level structure ==="
find . -maxdepth 2 -type d \
  | grep -v '.git' | grep -v 'node_modules' | grep -v '.venv' \
  | sort
```

### Check 2 — Repository Path References

Workflows reference repo paths in prose (e.g., "`src/auth/`",
"`domains/example/`", "`server_registry.yaml`"). Rather than maintaining a
static list — which itself can fall out of date — extract path references
directly from the workflow source files so the check always reflects what the
workflows actually say.

```bash
echo "=== Extract and verify path references from workflow files ==="
# Extract backtick-quoted tokens that look like repo-relative paths:
# contains at least one '/', is not a URL, not an Actions expression,
# not a glob pattern or template placeholder, not a file:line reference.
find .github/workflows/ -maxdepth 1 -name '*.md' -type f \
  | xargs grep -hoP '`(?!https?://)[A-Za-z0-9_.][A-Za-z0-9_./-]*/[^`\s]*`' \
  | tr -d '`' \
  | grep -v '\${{' \
  | grep -v '[*<>]' \
  | grep -vP ':\d+$' \
  | sort -u \
  | while IFS= read -r p; do
      if [ -e "$p" ]; then
        echo "  ✓ $p"
      else
        echo "  ✗ MISSING: $p"
      fi
    done
```

Flag any path reported as `MISSING` — investigate whether it was renamed,
moved, or deleted and update the referencing workflow accordingly.

### Check 3 — Architecture Descriptions & Tables

Some workflows embed architectural tables or descriptions (layer names,
module purposes, convention summaries). For each:

1. Verify that the described directory structure matches reality.
2. Check that key files or modules mentioned still exist and serve the
   described purpose (e.g., by reading their docstrings or top-level
   comments).
3. Flag descriptions that no longer match — e.g., layers that have been
   reorganised, modules that have been merged or split.

### Check 4 — Configuration & Convention Claims

Workflows state conventions (Python version, package manager, test commands,
markers, frameworks). Verify each against the actual config:

```bash
echo "=== Python version & tooling ==="
grep -i 'python' src/pyproject.toml 2>/dev/null | head -5
echo "---"
echo "=== Test markers ==="
grep -A 5 '\[tool.pytest' src/pyproject.toml 2>/dev/null | head -15
echo "---"
echo "=== Key dependencies ==="
grep -iE 'pydantic|fastmcp|mcp|agent-framework' src/pyproject.toml 2>/dev/null | head -10
```

Flag claims that conflict with actual config (e.g., wrong Python version,
removed dependencies, changed test commands).

### Check 5 — GitHub Actions Expressions & Event Filters

Workflows use `if:` conditionals and GitHub Actions expressions with event
properties (e.g., `github.event.label.name`, `github.event.comment.body`).

1. Verify that label names referenced in `if:` conditions match labels that
   actually exist in the repository.
2. Check that event types and property paths are valid per the GitHub Actions
   documentation.

```bash
echo "=== Repo labels ==="
gh label list --limit 100 2>/dev/null || echo "(gh label list unavailable)"
```

### Check 6 — Safe-Output Consistency

The YAML frontmatter declares `safe-outputs` (allowed labels, issue prefixes,
assignees). Check that:

1. Labels listed in `allowed` or `labels` arrays exist in the repository.
2. Title prefixes are unique across workflows (no two workflows share the
   same prefix, which would cause `close-older-issues` to close the wrong
   issues).

### Check 7 — Cross-Workflow Coherence

Compare instructions across all workflow files for contradictions:

1. Do different workflows give conflicting instructions about the same topic
   (e.g., test commands, directory structure, naming conventions)?
2. Do scheduled workflows overlap in a way that could cause conflicts (e.g.,
   two workflows creating issues with the same label and `close-older-issues:
   true`)?
3. Do workflows reference each other's labels, outputs, or side effects
   correctly?

## Report Generation

After all checks, triage findings:

1. **Discard false positives** — e.g., paths that are intentionally
   aspirational, expressions that are valid but use uncommon syntax,
   documentation references that point to external repos.
2. **Classify remaining findings** by severity:
   - 🔴 **Critical** — shell snippet would error or produce wrong results;
     workflow references a path or label that does not exist and would cause
     the workflow to malfunction.
   - 🟠 **High** — architecture description is materially wrong; convention
     claim contradicts actual config; cross-workflow conflict could cause
     unintended side effects.
   - 🟡 **Medium** — stale path reference that doesn't affect execution;
     minor description inaccuracy; outdated example.
   - 🔵 **Low** — cosmetic inconsistency between workflows; wording that is
     technically correct but confusing; style drift.

If **no actionable findings** remain after triage, **do not create an issue** —
simply exit.

## Issue Format

Create a single GitHub issue using `create-issue`. Structure the body as
follows:

```markdown
### Summary

One-paragraph overview: total findings, severity breakdown, and which
workflow files are most affected.

### 🔴 Critical — Broken Snippets or Missing References

For each finding:
- **Workflow**: `workflow-file.md`
- **What it says**: quote the relevant passage or snippet
- **What the repo shows**: evidence (actual paths, command output, etc.)
- **Suggested fix**: concrete change to the workflow source

(Omit section if empty.)

### 🟠 High — Materially Wrong Descriptions

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
- [ ] Fix critical findings first
- [ ] Correct high-severity description and config mismatches
- [ ] Update stale paths and examples
- [ ] Review low-severity items for quick wins

> **Note:** After editing any `.md` workflow file, run `gh aw compile` and
> commit the updated `.lock.yml` file.
```

## Rules

- Only create an issue when at least one finding survives triage. When
  deciding whether a finding is real, err on the side of inclusion — the
  coding agent can dismiss false positives, but it cannot fix problems it
  was never told about.
- Group related findings (e.g., the same stale path across multiple
  workflows) into a single item.
- Include enough context for the coding agent to fix each finding without
  re-running the audit.
- Keep the issue concise but thorough.
- Always remind the coding agent to recompile lock files after editing
  workflow source files.
