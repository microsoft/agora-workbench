#!/usr/bin/env bash
set -euo pipefail

wheel=$(find dist -maxdepth 1 -name '*.whl' -print -quit)
if [[ -z "$wheel" ]]; then
  echo "No wheel found in dist/." >&2
  exit 1
fi
extras="${AGORA_WORKBENCH_SMOKE_EXTRAS:-}"
install_target="$wheel"
if [[ -n "$extras" ]]; then
  install_target="${wheel}[${extras}]"
fi

venv="$PWD/.package-venv"
rm -rf "$venv"
scaffold=""
cleanup() {
  rm -rf "$venv"
  if [[ -n "$scaffold" ]]; then
    rm -rf "$scaffold"
  fi
}
trap cleanup EXIT

uv venv "$venv" --python 3.11
uv pip install --python "$venv/bin/python" "$install_target"

cd /tmp
"$venv/bin/python" - <<'PY'
from importlib.metadata import entry_points, version
from importlib.util import find_spec
import os
import tempfile
from pathlib import Path

import agora_workbench
import activity_ui
from agora_workbench import CodeExecutionServer
import agora_workbench.code_execution.auth as auth
from agora_workbench.code_execution.auth import create_noop_auth_config
from agora_workbench.code_execution.data_access import (
    AssetPublisher,
    AssetResolutionMiddleware,
    BlobPublisher,
    GuiPublisher,
    LocalFilePublisher,
    MsalCacheCredential,
    SearchIndexArtifactResolver,
    ServerPublisher,
    create_storage_credential,
)
from agora_workbench.code_execution.data_access.manager import DataLakeDataManager
import agora_workbench.code_execution.tools.search as tool_search
from agora_workbench.data_lake import CatalogDB

print(f"agora-workbench {version('agora-workbench')}")
print(agora_workbench.__file__)
print(activity_ui.__file__)
print(CodeExecutionServer.__name__)
print(type(create_noop_auth_config()).__name__)
print(
    ", ".join(
        value.__name__
        for value in (
            AssetPublisher,
            AssetResolutionMiddleware,
            BlobPublisher,
            GuiPublisher,
            LocalFilePublisher,
            MsalCacheCredential,
            SearchIndexArtifactResolver,
            ServerPublisher,
            create_storage_credential,
        )
    )
)

data_manager = DataLakeDataManager()
data_manager.cleanup()

full_install = bool(os.getenv("AGORA_WORKBENCH_SMOKE_EXTRAS"))
assert "noop" in vars(auth)
assert "base" in vars(auth)
assert "bm25_tool_search" in vars(tool_search)
assert "state_graph" in vars(tool_search)
assert auth.noop.__name__.endswith(".auth.noop")
assert auth.base.__name__.endswith(".auth.base")
assert tool_search.bm25_tool_search.__name__.endswith(".search.bm25_tool_search")
assert tool_search.state_graph.__name__.endswith(".search.state_graph")

if full_install:
    for optional_package in ("azure", "openai", "sqlite_vec"):
        if find_spec(optional_package) is None:
            raise SystemExit(f"Full wheel is missing optional package: {optional_package}")
    assert "entra" in vars(auth)
    assert "azure_credentials" in vars(auth)
    assert "EntraCredentialProvider" in vars(auth)
    assert "azure_ai_tool_search" in vars(tool_search)
    assert "AzureAIToolSearchBackend" in vars(tool_search)
    from agora_workbench.code_execution.auth.entra import (
        EntraCredentialProvider as DirectEntraCredentialProvider,
    )
    from agora_workbench.code_execution.tools.search.azure_ai_tool_search import (
        AzureAIToolSearchBackend as DirectAzureAIToolSearchBackend,
    )

    assert auth.EntraCredentialProvider is DirectEntraCredentialProvider
    assert tool_search.AzureAIToolSearchBackend is DirectAzureAIToolSearchBackend
    assert auth.entra.__name__.endswith(".auth.entra")
    assert auth.azure_credentials.__name__.endswith(".auth.azure_credentials")
    assert tool_search.azure_ai_tool_search.__name__.endswith(".search.azure_ai_tool_search")
else:
    for optional_package in ("azure", "openai", "sqlite_vec"):
        if find_spec(optional_package) is not None:
            raise SystemExit(f"Base wheel unexpectedly installed optional package: {optional_package}")
    for module_owner, attribute in (
        (auth, "entra"),
        (auth, "azure_credentials"),
        (auth, "EntraCredentialProvider"),
        (tool_search, "azure_ai_tool_search"),
        (tool_search, "AzureAIToolSearchBackend"),
    ):
        try:
            getattr(module_owner, attribute)
        except ImportError:
            pass
        else:
            raise SystemExit(f"Base wheel unexpectedly imported optional submodule: {attribute}")

with tempfile.TemporaryDirectory() as directory:
    db_path = Path(directory) / "catalog.db"
    catalog = CatalogDB(db_path)
    catalog.open()
    if catalog.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'artifacts_vec'"
    ).fetchone() is not None:
        raise SystemExit("Base wheel unexpectedly created a vector table")
    catalog.upsert_artifact(
        artifact_id="weather",
        name="weather.csv",
        storage_uri="/data/weather.csv",
        description="Local weather observations",
        indexed_at="2026-01-01T00:00:00Z",
    )
    catalog.close()

    reopened = CatalogDB(db_path)
    reopened.open()
    if reopened.search("weather")[0].id != "weather":
        raise SystemExit("Base wheel keyword catalog smoke test failed")
    reopened.close()

if full_install:
    vector_catalog = CatalogDB(":memory:", vec_dimensions=2)
    vector_catalog.open()
    vector_catalog.upsert_artifact(
        artifact_id="vector",
        name="vector.csv",
        storage_uri="/data/vector.csv",
        indexed_at="2026-01-01T00:00:00Z",
        embedding=[1.0, 0.0],
    )
    if vector_catalog.search("", query_embedding=[1.0, 0.0])[0].id != "vector":
        raise SystemExit("Full wheel vector catalog smoke test failed")
    vector_catalog.close()

scripts = {entry.name: entry for entry in entry_points(group="console_scripts")}
for name in ("mcp-connector-server", "agora-workbench-deploy"):
    if name not in scripts:
        raise SystemExit(f"Missing console entry point: {name}")
    if not callable(scripts[name].load()):
        raise SystemExit(f"Console entry point is not callable: {name}")
PY

scaffold=$(mktemp -d)
"$venv/bin/agora-workbench-deploy" init --target activity-ui --output-dir "$scaffold"
test -f "$scaffold/activity_ui/Dockerfile"
test -f "$scaffold/activity_ui/server.py"
test -f "$scaffold/activity_ui/static/index.html"
