"""Administrative CLI and runnable data-lake example tests."""

from __future__ import annotations

import hashlib
import importlib.abc
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from agora_workbench.data_lake import cli
from agora_workbench.data_lake.cli import _close_optional_resource, main
from agora_workbench.data_lake.catalog import CatalogConfig
from agora_workbench.data_lake.manifest import CatalogManifest

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]


def _scan_config(root: Path, config: Path) -> None:
    config.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "sources": [
                    {
                        "source_id": "local",
                        "path": str(root),
                        "discovery": "scan",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_cli_init_validate_refresh_and_search(tmp_path, capsys):
    source = tmp_path / "data"
    source.mkdir()
    (source / "weather.csv").write_text("day,temp\n2026-01-01,7\n", encoding="utf-8")
    config = tmp_path / "catalog.yaml"
    database = tmp_path / "catalog.db"

    assert main(["init", "--config", str(config), "--source", str(source), "--source-id", "local"]) == 0
    assert main(["validate", "--config", str(config)]) == 0
    assert main(["refresh", "--config", str(config), "--database", str(database)]) == 0
    assert (
        main(
            [
                "search",
                "weather",
                "--config",
                str(config),
                "--database",
                str(database),
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert '"name": "weather.csv"' in output


def test_cli_invalid_manifest_fails_without_scan_fallback(tmp_path, capsys):
    source = tmp_path / "data"
    source.mkdir()
    (source / "unregistered.csv").write_text("must not be scanned", encoding="utf-8")
    (source / "manifest.json").write_text("{", encoding="utf-8")
    config = tmp_path / "catalog.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "sources": [
                    {
                        "source_id": "approved",
                        "path": str(source),
                        "discovery": "manifest",
                        "manifest": "manifest.json",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert main(["validate", "--config", str(config)]) == 1
    streams = capsys.readouterr()
    assert "manifest_content_valid" in streams.out
    assert "Catalog validation failed" in streams.err
    assert "unregistered.csv" not in streams.out


def test_cli_failure_paths_are_nonzero_and_actionable(tmp_path, capsys):
    missing = tmp_path / "missing.yaml"
    assert main(["validate", "--config", str(missing)]) == 1
    assert f"Catalog config not found: {missing}" in capsys.readouterr().err

    root = tmp_path / "lake"
    root.mkdir()
    external = root / "external.csv"
    external.write_text("data", encoding="utf-8")
    checksum = hashlib.sha256(external.read_bytes()).hexdigest()
    arguments = [
        "register",
        "--root",
        str(root),
        "--source-id",
        "managed",
        "--operation-id",
        "register-1",
        "--path",
        "approved/data.csv",
        "--storage-path",
        "external.csv",
        "--checksum-sha256",
        checksum,
    ]
    assert main(arguments) == 1
    assert "requires application authorization" in capsys.readouterr().err
    assert main([*arguments, "--allow-development-writes"]) == 0


def test_cli_init_failure_does_not_write_partial_config(tmp_path, capsys):
    config = tmp_path / "catalog.yaml"
    assert (
        main(
            [
                "init",
                "--config",
                str(config),
                "--source",
                "az://exampleaccount/example-container/data/",
                "--source-id",
                "approved",
                "--discovery",
                "manifest",
                "--manifest",
                ".agora/manifest.json",
                "--create-empty-manifest",
            ]
        )
        == 1
    )
    assert "never provisions cloud resources" in capsys.readouterr().err
    assert not config.exists()


@pytest.mark.parametrize("absolute", [False, True])
def test_cli_init_rejects_manifest_outside_source(tmp_path, capsys, absolute):
    source = tmp_path / "source"
    outside = tmp_path / "outside" / "manifest.json"
    manifest = str(outside) if absolute else "../outside/manifest.json"
    config = tmp_path / "catalog.yaml"

    result = main(
        [
            "init",
            "--config",
            str(config),
            "--source",
            str(source),
            "--source-id",
            "approved",
            "--discovery",
            "manifest",
            "--manifest",
            manifest,
            "--create-empty-manifest",
        ]
    )

    assert result == 1
    assert "must stay within" in capsys.readouterr().err
    assert not config.exists()
    assert not outside.exists()


def test_cli_init_rejects_config_manifest_alias_before_writes(tmp_path, capsys):
    source = tmp_path / "source"
    config = source / "nested" / ".." / "catalog.yaml"

    result = main(
        [
            "init",
            "--config",
            str(config),
            "--source",
            str(source),
            "--source-id",
            "approved",
            "--discovery",
            "manifest",
            "--manifest",
            "catalog.yaml",
            "--create-empty-manifest",
        ]
    )

    assert result == 1
    assert "must refer to different files" in capsys.readouterr().err
    assert not (source / "catalog.yaml").exists()


def test_cli_init_manifest_stage_failure_preserves_prior_files_and_retries(tmp_path, monkeypatch, capsys):
    source = tmp_path / "source"
    source.mkdir()
    config = tmp_path / "catalog.yaml"
    manifest = source / "manifest.json"
    config.write_text("prior config\n", encoding="utf-8")
    manifest.write_text("prior manifest\n", encoding="utf-8")
    original_stage = cli._stage_text
    fail_once = True

    def fail_manifest_stage(destination, content, token):
        nonlocal fail_once
        if fail_once and destination == manifest:
            fail_once = False
            raise OSError("injected manifest write failure")
        return original_stage(destination, content, token)

    monkeypatch.setattr(cli, "_stage_text", fail_manifest_stage)
    arguments = [
        "init",
        "--config",
        str(config),
        "--source",
        str(source),
        "--source-id",
        "approved",
        "--discovery",
        "manifest",
        "--manifest",
        "manifest.json",
        "--create-empty-manifest",
        "--force",
    ]

    assert main(arguments) == 1
    assert "injected manifest write failure" in capsys.readouterr().err
    assert config.read_text(encoding="utf-8") == "prior config\n"
    assert manifest.read_text(encoding="utf-8") == "prior manifest\n"

    assert main(arguments) == 0
    assert CatalogConfig.from_yaml(config).sources[0].manifest == "manifest.json"
    assert CatalogManifest.from_mapping(json.loads(manifest.read_text(encoding="utf-8"))).generation == 1


def test_refresh_missing_local_source_prints_state_and_fails(tmp_path, capsys):
    config = tmp_path / "catalog.yaml"
    database = tmp_path / "catalog.db"
    _scan_config(tmp_path / "missing", config)

    assert main(["refresh", "--config", str(config), "--database", str(database)]) == 1
    streams = capsys.readouterr()
    output = json.loads(streams.out)
    assert output["sources"][0]["status"] == "error"
    assert output["sources"][0]["error"] == "source_missing: configured source was not found"
    assert "exists and is readable" in output["hint"]
    assert "inspect the structured source states" in streams.err


def test_refresh_missing_azure_extra_has_actionable_sanitized_hint(tmp_path, monkeypatch, capsys):
    class BlockAzure(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "azure" or fullname.startswith("azure."):
                raise ModuleNotFoundError("blocked Azure import with SECRET", name=fullname)
            return None

    monkeypatch.setattr(sys, "meta_path", [BlockAzure(), *sys.meta_path])
    config = tmp_path / "catalog.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "sources": [
                    {
                        "source_id": "azure",
                        "path": "az://exampleaccount/example-container/data/",
                        "discovery": "scan",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert main(["refresh", "--config", str(config), "--database", str(tmp_path / "catalog.db")]) == 1
    streams = capsys.readouterr()
    output = json.loads(streams.out)
    assert output["sources"][0]["error"] == "optional_dependency_missing: source refresh failed"
    assert "uv add 'agora-workbench[azure]'" in output["hint"]
    assert "SECRET" not in streams.out
    assert "SECRET" not in streams.err


def test_keyword_cli_does_not_import_optional_backends(tmp_path, monkeypatch):
    blocked = ("azure", "openai", "sqlite_vec")

    class BlockOptionalImports(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if any(fullname == name or fullname.startswith(f"{name}.") for name in blocked):
                raise ModuleNotFoundError(f"blocked optional import: {fullname}", name=fullname)
            return None

    monkeypatch.setattr(sys, "meta_path", [BlockOptionalImports(), *sys.meta_path])
    source = tmp_path / "data"
    source.mkdir()
    (source / "weather.csv").write_text("data", encoding="utf-8")
    config = tmp_path / "catalog.yaml"
    database = tmp_path / "catalog.db"
    _scan_config(source, config)

    assert main(["validate", "--config", str(config)]) == 0
    assert main(["refresh", "--config", str(config), "--database", str(database)]) == 0
    assert main(["search", "weather", "--config", str(config), "--database", str(database)]) == 0


@pytest.mark.parametrize("index_error", [None, RuntimeError("refresh failed")])
def test_refresh_closes_initialized_async_embedding_provider(tmp_path, monkeypatch, index_error):
    config = tmp_path / "catalog.yaml"
    config.write_text("version: 1\nsources: []\n", encoding="utf-8")
    provider = SimpleNamespace(closed=False)

    async def aclose():
        provider.closed = True

    provider.aclose = aclose

    class FakeIndexer:
        def __init__(self, config, db):
            pass

        @property
        def embedding_provider(self):
            return provider

        async def index(self):
            if index_error is not None:
                raise index_error
            return 0

    monkeypatch.setattr(cli, "CatalogIndexer", FakeIndexer)
    result = main(["refresh", "--config", str(config), "--database", str(tmp_path / "catalog.db")])

    assert result == (1 if index_error is not None else 0)
    assert provider.closed


async def test_cleanup_helper_prefers_aclose_and_accepts_sync_cleanup():
    sync_calls = []

    class SyncResource:
        def close(self):
            sync_calls.append("close")

    await _close_optional_resource(SyncResource())
    assert sync_calls == ["close"]

    preferred_calls = []

    class PreferredResource:
        def aclose(self):
            preferred_calls.append("aclose")

        def close(self):
            preferred_calls.append("close")

    await _close_optional_resource(PreferredResource())
    assert preferred_calls == ["aclose"]


def test_reconcile_prints_sanitized_failure_details_and_returns_nonzero(tmp_path, monkeypatch, capsys):
    root = tmp_path / "lake"
    root.mkdir()

    class FailingWriter:
        def __init__(self, source_id, backend):
            pass

        async def reconcile(self, *, grace_seconds):
            raise cli.ReconciliationError(
                "generic failure without SECRET backend detail",
                failures={"operation-7": "RuntimeError"},
                operation="reconcile",
            )

    monkeypatch.setattr(cli, "ManagedCatalogWriter", FailingWriter)

    assert main(["reconcile", "--root", str(root), "--source-id", "managed"]) == 1
    streams = capsys.readouterr()
    assert json.loads(streams.out)["failures"] == {"operation-7": "RuntimeError"}
    assert "SECRET" not in streams.out
    assert "SECRET" not in streams.err


def test_public_api_examples_run_from_clean_workspaces(tmp_path):
    local_script = REPOSITORY_ROOT / "examples/data_lake/local_read_only.py"
    managed_script = REPOSITORY_ROOT / "examples/data_lake/managed_promotion.py"
    for discovery in ("scan", "manifest"):
        result = subprocess.run(
            [
                sys.executable,
                str(local_script),
                "--workspace",
                str(tmp_path / discovery),
                "--discovery",
                discovery,
            ],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["discovery"] == discovery

    result = subprocess.run(
        [sys.executable, str(managed_script), "--workspace", str(tmp_path / "managed")],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["generation"] == 1


def test_azure_examples_are_versioned_and_credential_free():
    config_path = REPOSITORY_ROOT / "examples/data_lake/catalog.azure.yaml"
    manifest_path = REPOSITORY_ROOT / "examples/data_lake/azure.manifest.example.json"
    config = CatalogConfig.from_yaml(config_path)
    manifest = CatalogManifest.from_mapping(json.loads(manifest_path.read_text(encoding="utf-8")))

    assert config.sources[0].path == "az://exampleaccount/example-container/approved/"
    assert config.search.embedding_model == "none"
    assert manifest.version == 1
    assert manifest.generation == 1
    assert "sig=" not in config_path.read_text(encoding="utf-8").lower()
