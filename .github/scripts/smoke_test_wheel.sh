#!/usr/bin/env bash
set -euo pipefail

wheel=$(find dist -maxdepth 1 -name '*.whl' -print -quit)
if [[ -z "$wheel" ]]; then
  echo "No wheel found in dist/." >&2
  exit 1
fi

venv="$PWD/.package-venv"
rm -rf "$venv"
uv venv "$venv" --python 3.11
uv pip install --python "$venv/bin/python" "$wheel"

cd /tmp
"$venv/bin/python" - <<'PY'
from importlib.metadata import entry_points, version

import agora_workbench

print(f"agora-workbench {version('agora-workbench')}")
print(agora_workbench.__file__)

scripts = {entry.name: entry for entry in entry_points(group="console_scripts")}
for name in ("mcp-connector-server", "agora-workbench-deploy"):
    if name not in scripts:
        raise SystemExit(f"Missing console entry point: {name}")
    if not callable(scripts[name].load()):
        raise SystemExit(f"Console entry point is not callable: {name}")
PY
