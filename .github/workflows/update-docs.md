---
description: |
  This workflow keeps docs synchronized with code changes.
  Runs weekly on Sundays to review recent commits and PRs for documentation gaps, and opens a GitHub issue when updates are needed.

on:
  schedule: weekly on sunday
  workflow_dispatch:

permissions:
  contents: read
  issues: read
  pull-requests: read

safe-outputs:
  create-issue:
    assignees: [copilot]
    close-older-issues: true

tools:
  github:
    toolsets: [repos, pull_requests, issues]

timeout-minutes: 5
source: githubnext/agentics/workflows/update-docs.md@1f672aef974f4246124860fc532f82fe8a93a57e
---
# Update Docs

You are a workflow trigger for the `${{ github.repository }}` repository.

Your only job is to review whether documentation may need updating, and if so, open a GitHub issue to request the work.

## Steps

1. **Analyze recent changes** — look at commits and PRs merged since the last run to determine if any documentation may be out of date or missing.
2. **If updates are needed**, create a GitHub issue with:
   - A clear title summarizing the documentation gap (e.g. "Docs: update README for new X feature").
   - A body that lists the specific files or areas that need attention and why.
   - The label `documentation`.
   - End the issue body with a note: `> **Note:** Follow the documentation guidelines in .github/agents/documentation-agent.md.`
3. **If no updates are needed**, call `noop` with a brief message such as "No documentation updates needed — all recent changes are already covered in the docs."

> NOTE: Do NOT create pull requests or edit files directly. Your only output is a GitHub issue assigned to Copilot (assignment is handled automatically).
