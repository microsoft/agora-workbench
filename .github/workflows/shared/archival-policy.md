## Archival Path Exclusions

This is a **reusable prompt fragment** for Copilot agent workflows. Include
it via `{{#runtime-import}}` or copy the snippet below into your workflow.

Before performing any scans, analysis, or code modifications, read the file
`.github/archival-paths` in the repository root. This file lists directories
that are **archival or legacy code** and must be excluded from your work.

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

### Usage in commands

- **grep**: `grep -rn "${ARCHIVAL_GREP_EXCLUDES[@]}" --include='*.py' ...`
- **find**: `find . \( "${ARCHIVAL_FIND_PRUNE[@]}" \) -prune -o -name '*.py' -print0`

### Rules for archival paths

- **Do not scan** files under archival paths for issues.
- **Do not propose changes** to files under archival paths.
- **Do not report findings** from archival paths — they are expected to contain
  legacy patterns and are intentionally preserved as-is.
- If a finding in active code *references* archival code (e.g., an import),
  report the finding against the active file only.
