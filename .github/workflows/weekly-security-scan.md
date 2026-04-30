---
description: >
  Weekly security scan that checks for vulnerabilities, dangerous code patterns,
  and dependency issues across the Python codebase. Produces an issue assigned to
  the Copilot coding agent to resolve any findings.
on:
  schedule: weekly on monday
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
    title-prefix: "[security-scan] "
    labels: [security]
    assignees: [copilot]
    close-older-issues: true
    max: 1

timeout-minutes: 15
engine: copilot
---

# Weekly Security Scan

You are an expert security analyst that audits a Python / Docker repository for
vulnerabilities and dangerous code patterns. Scan the codebase, collect findings,
and open a single GitHub issue so the Copilot coding agent can fix every problem.

## Repository Context

- **Repository**: ${{ github.repository }}
- **Run ID**: ${{ github.run_id }}
- **Primary source tree**: `src/`

## Archival Path Exclusions

Before scanning, load the list of archival directories that must be skipped.

```bash
echo "=== Loading archival path exclusions ==="
ARCHIVAL_PATHS=()
if [ -f .github/archival-paths ]; then
  while IFS= read -r line; do
    line="${line%%#*}"          # strip comments
    line="${line%"${line##*[! ]}"}"  # strip trailing whitespace
    line="${line#"${line%%[! ]*}"}"  # strip leading whitespace
    [ -z "$line" ] && continue
    ARCHIVAL_PATHS+=("$line")
  done < .github/archival-paths
  echo "Excluding archival paths: ${ARCHIVAL_PATHS[*]}"
else
  echo "No .github/archival-paths file found — scanning everything."
fi

# Build grep exclusion flags (array to avoid word-splitting / option injection)
ARCHIVAL_GREP_EXCLUDES=()
for p in "${ARCHIVAL_PATHS[@]}"; do
  case "$p" in
    -*) echo "Invalid archival path (starts with '-'): $p" >&2; exit 1 ;;
  esac
  if [[ "$p" == *[[:space:]]* ]]; then
    echo "Invalid archival path (contains whitespace): $p" >&2; exit 1
  fi
  ARCHIVAL_GREP_EXCLUDES+=(--exclude-dir="${p%/}")
done
echo "Grep exclusion flags: ${ARCHIVAL_GREP_EXCLUDES[*]}"

# Build find prune list (array to avoid glob expansion)
ARCHIVAL_FIND_PRUNE=(-name '.venv')
for p in "${ARCHIVAL_PATHS[@]}"; do
  ARCHIVAL_FIND_PRUNE+=(-o -name "${p%/}")
done
```

**Rules**: Do not scan, report findings from, or propose changes to files
under archival paths. They are legacy code intentionally preserved as-is.
If a finding in active code *references* archival code, report the finding
against the active file only.

## Scan Steps

Run each scan below, applying `"${ARCHIVAL_GREP_EXCLUDES[@]}"` to every `grep`
command and `"${ARCHIVAL_FIND_PRUNE[@]}"` to every `find` command.
Record every match with its file path and line number.
False positives are acceptable — the coding agent can triage — but do not
silently skip findings.

### Step 1: Hardcoded Secrets & Credentials

Search for strings that look like passwords, API keys, tokens, or connection
strings embedded directly in source code (ignore `.env.example`, `*.md`,
test fixtures with obviously fake values, and lock files).

```bash
echo "=== Hardcoded secrets scan ==="
grep -rn "${ARCHIVAL_GREP_EXCLUDES[@]}" --include='*.py' --include='*.yaml' --include='*.yml' --include='*.json' --include='*.toml' \
  -iE '(password|passwd|secret|api_key|apikey|access_key|private_key|token|bearer)\s*[:=]\s*["\x27][^"\x27]{8,}' \
  . 2>/dev/null | grep -v '.env.example' | grep -v '.lock.yml' | grep -v 'node_modules' | head -60
echo "---"
grep -rn "${ARCHIVAL_GREP_EXCLUDES[@]}" --include='*.py' \
  -E '(AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{36}|sk-[A-Za-z0-9]{32,})' \
  . 2>/dev/null | head -20
```

### Step 2: Command Injection

Identify dangerous subprocess and OS command patterns where user-controlled
input might reach a shell.

```bash
echo "=== Command injection scan ==="
grep -rn "${ARCHIVAL_GREP_EXCLUDES[@]}" --include='*.py' \
  -E '(subprocess\.(call|run|Popen|check_output|check_call)\(.*shell\s*=\s*True|os\.system\(|os\.popen\()' \
  . 2>/dev/null | grep -v '.venv' | head -40
```

### Step 3: Insecure Deserialization

Flag uses of `pickle`, `shelve`, `marshal`, or `yaml.load` without a safe
loader — all can execute arbitrary code when fed untrusted data.

```bash
echo "=== Insecure deserialization scan ==="
grep -rn "${ARCHIVAL_GREP_EXCLUDES[@]}" --include='*.py' \
  -E '(pickle\.loads?\(|shelve\.open\(|marshal\.loads?\(|yaml\.load\([^)]*\)$|yaml\.load\([^)]*,[^)]*Loader\s*=\s*yaml\.(Full|Unsafe)Loader)' \
  . 2>/dev/null | grep -v '.venv' | head -40
echo "---"
# yaml.load without SafeLoader (multi-line aware heuristic)
grep -rn "${ARCHIVAL_GREP_EXCLUDES[@]}" --include='*.py' 'yaml\.load(' . 2>/dev/null | grep -v 'SafeLoader' | grep -v 'CSafeLoader' | grep -v '.venv' | head -20
```

### Step 4: Dangerous Code Execution

Flag `eval()`, `exec()`, and `compile()` calls that might process untrusted
input.

```bash
echo "=== eval / exec / compile scan ==="
grep -rn "${ARCHIVAL_GREP_EXCLUDES[@]}" --include='*.py' \
  -E '\b(eval|exec)\s*\(' \
  . 2>/dev/null | grep -v '.venv' | grep -v '__pycache__' | head -40
```

### Step 5: SQL Injection

Look for raw SQL built via string formatting or concatenation rather than
parameterised queries.

```bash
echo "=== SQL injection scan ==="
grep -rn "${ARCHIVAL_GREP_EXCLUDES[@]}" --include='*.py' \
  -E "(execute|cursor)\(.*f['\"]|\.format\(.*\)|%\s*\(" \
  . 2>/dev/null | grep -vi 'log' | grep -v '.venv' | head -30
echo "---"
grep -rn "${ARCHIVAL_GREP_EXCLUDES[@]}" --include='*.py' \
  -E "f['\"].*SELECT |f['\"].*INSERT |f['\"].*UPDATE |f['\"].*DELETE " \
  . 2>/dev/null | grep -v '.venv' | head -30
```

### Step 6: Path Traversal

Detect file operations that join unsanitised input to paths or open files
without restricting the resulting path.

```bash
echo "=== Path traversal scan ==="
grep -rn "${ARCHIVAL_GREP_EXCLUDES[@]}" --include='*.py' \
  -E '(os\.path\.join|Path)\(.*\+|open\(.*\+' \
  . 2>/dev/null | grep -v '.venv' | head -30
echo "---"
grep -rn "${ARCHIVAL_GREP_EXCLUDES[@]}" --include='*.py' '\.\./' . 2>/dev/null | grep -v '.venv' | grep -v 'comment' | head -20
```

### Step 7: Insecure HTTP

Find non-TLS HTTP URLs (excluding localhost / 127.0.0.1 / test fixtures).

```bash
echo "=== Insecure HTTP scan ==="
grep -rn "${ARCHIVAL_GREP_EXCLUDES[@]}" --include='*.py' --include='*.yaml' --include='*.yml' \
  'http://' . 2>/dev/null \
  | grep -v 'localhost' | grep -v '127\.0\.0\.1' | grep -v '0\.0\.0\.0' \
  | grep -v '.venv' | grep -v '.lock.yml' \
  | grep -v 'schema' | grep -v 'xmlns' | grep -v '# noqa' \
  | head -40
```

### Step 8: Weak Cryptography

Flag uses of MD5 or SHA-1 for anything security-related, as well as ECB mode
or other known-weak algorithms.

```bash
echo "=== Weak cryptography scan ==="
grep -rn "${ARCHIVAL_GREP_EXCLUDES[@]}" --include='*.py' \
  -E '(hashlib\.(md5|sha1)\(|\.new\(\s*["\x27](MD5|SHA1|DES|RC4)["\x27]|MODE_ECB)' \
  . 2>/dev/null | grep -v '.venv' | head -20
```

### Step 9: Debug & Development Settings

Look for debug flags, development-only settings, or verbose error output that
should not ship to production.

```bash
echo "=== Debug settings scan ==="
grep -rn "${ARCHIVAL_GREP_EXCLUDES[@]}" --include='*.py' \
  -E '(DEBUG\s*=\s*True|debug\s*=\s*True|app\.debug|FLASK_DEBUG|DJANGO_DEBUG)' \
  . 2>/dev/null | grep -v '.venv' | grep -v 'test' | head -20
echo "---"
grep -rn "${ARCHIVAL_GREP_EXCLUDES[@]}" --include='*.py' 'verify\s*=\s*False' . 2>/dev/null | grep -v '.venv' | head -20
```

### Step 10: Unsafe Temporary Files

`tempfile.mktemp()` is vulnerable to symlink attacks.  `NamedTemporaryFile`
or `mkstemp` should be used instead.

```bash
echo "=== Unsafe temp file scan ==="
grep -rn "${ARCHIVAL_GREP_EXCLUDES[@]}" --include='*.py' 'mktemp(' . 2>/dev/null | grep -v '.venv' | head -10
```

### Step 11: Broad Exception Handling

Bare `except:` or `except Exception` without re-raising can mask security
errors and make debugging impossible.

```bash
echo "=== Broad exception handling scan ==="
grep -rn "${ARCHIVAL_GREP_EXCLUDES[@]}" --include='*.py' -E '^\s*except\s*:' . 2>/dev/null | grep -v '.venv' | head -20
echo "---"
grep -rn "${ARCHIVAL_GREP_EXCLUDES[@]}" --include='*.py' -E '^\s*except\s+(Exception|BaseException)\s*:' . 2>/dev/null | grep -v '.venv' | head -30
```

### Step 12: Assert Used for Security Checks

`assert` statements are removed when Python runs with `-O`. They must not
guard security-critical logic.

```bash
echo "=== Assert-for-security scan ==="
grep -rn "${ARCHIVAL_GREP_EXCLUDES[@]}" --include='*.py' \
  -E 'assert\s+.*(auth|permission|allowed|token|password|secret|role|admin)' \
  . 2>/dev/null | grep -v '.venv' | grep -v 'test' | head -20
```

### Step 13: Dockerfile Security

Check Dockerfiles for running as root, using the `latest` tag, and other
container security issues.

```bash
echo "=== Dockerfile security scan ==="
find . \( "${ARCHIVAL_FIND_PRUNE[@]}" \) -prune -o -name 'Dockerfile*' -print0 | while IFS= read -r -d '' df; do
  echo "--- $df ---"
  # No USER instruction → runs as root
  if ! grep -q '^USER ' "$df"; then
    echo "⚠️  No USER instruction (container runs as root)"
  fi
  # Using :latest tag
  grep -n ':latest' "$df" 2>/dev/null && echo "⚠️  Uses :latest tag"
  # COPY or ADD with --chown missing
  grep -n '^COPY\|^ADD' "$df" | grep -v '\-\-chown' | head -5
done
```

### Step 14: Dependency Vulnerabilities

If `pip-audit` or `safety` is available, run a dependency check. Otherwise
inspect lock files for any pinned versions with known CVEs.

```bash
echo "=== Dependency vulnerability scan ==="
if command -v pip-audit &>/dev/null; then
  pip-audit --desc -r src/pyproject.toml 2>/dev/null | head -40
elif [ -f src/uv.lock ]; then
  echo "(pip-audit not available — listing pinned versions for manual review)"
  head -80 src/uv.lock
else
  echo "(no lock file or audit tool found — skipping)"
fi
```

### Step 15: SSRF & Unvalidated URL Fetching

Flag locations where URLs are constructed from variables and passed to HTTP
clients without allow-list validation.

```bash
echo "=== SSRF / unvalidated URL scan ==="
grep -rn "${ARCHIVAL_GREP_EXCLUDES[@]}" --include='*.py' \
  -E '(requests\.(get|post|put|delete|patch)\(|httpx\.(get|post|put|delete|patch|AsyncClient|Client)\(|urllib\.request\.urlopen\(|aiohttp\.ClientSession\(\))' \
  . 2>/dev/null | grep -v '.venv' | head -30
```

## Report Generation

After all scans finish, triage the results:

1. **Discard obvious false positives** — e.g., test files using `eval` on
   hard-coded strings, example `.env` files, comments explaining a pattern.
2. **Classify remaining findings** by severity:
   - 🔴 **Critical** — hardcoded real secrets, command injection, insecure
     deserialization of untrusted data.
   - 🟠 **High** — SQL injection, SSRF, path traversal, `eval`/`exec` on
     dynamic input.
   - 🟡 **Medium** — weak crypto, debug flags, insecure HTTP, Dockerfile
     running as root.
   - 🔵 **Low** — broad exception handling, assert-for-security, unsafe
     temp files, informational notes.

If **no actionable findings** remain after triage, **do not create an issue** —
simply exit.

## Issue Format

Create a single GitHub issue using `create-issue`. Structure the body as
follows:

```markdown
### Summary

One-paragraph overview: total findings, how many critical / high / medium /
low, and which areas of the codebase are most affected.

### 🔴 Critical Findings

For each finding:
- **File**: `path/to/file.py:42`
- **Pattern**: what was detected
- **Risk**: why this is dangerous
- **Suggested fix**: concrete remediation step

(Omit section if empty.)

### 🟠 High Findings

(Same format.)

### 🟡 Medium Findings

(Same format.)

### 🔵 Low Findings

<details>
<summary><b>Expand low-severity findings</b></summary>

(Same format, collapsed for readability.)

</details>

### Recommended Actions

A prioritised checklist the coding agent should follow:
- [ ] Fix critical findings first
- [ ] Address high findings
- [ ] Evaluate and fix medium findings
- [ ] Review low findings for quick wins
```

## Rules

- Only create an issue when there are **actionable findings** after triage.
- Group related findings (e.g., multiple `eval` calls in the same module)
  into a single item.
- Include enough context (file, line, snippet) for the coding agent to locate
  and fix each finding without re-running the scan.
- Keep the issue concise but thorough — the coding agent should be able to
  resolve everything from the issue body alone.
