"""Administrative CLI and runnable data-lake example tests."""

from __future__ import annotations

import hashlib
import importlib.abc
import json
import subprocess
import sys
from pathlib import Path

import yaml

from agora_workbench.data_lake.cli import main
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


def test_keyword_cli_does_not_require_unrelated_optional_configuration(tmp_path):
    source = tmp_path / "data"
    source.mkdir()
    (source / "weather.csv").write_text("data", encoding="utf-8")
    config = tmp_path / "catalog.yaml"
    database = tmp_path / "catalog.db"
    _scan_config(source, config)

    assert main(["refresh", "--config", str(config), "--database", str(database)]) == 0
    assert main(["search", "weather", "--config", str(config), "--database", str(database)]) == 0


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
