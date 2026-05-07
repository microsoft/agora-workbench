"""Unit tests for the data-lake ingestion pipeline.

Covers:
    - Manifest Pydantic models  (data_lake.manifest)
  - Ingestion orchestrator     (data_lake.ingest.orchestrator)
  - Artifact registry sync     (data_lake.sync.sync)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest
import yaml

from data_lake.manifest.manifest import (
    DataConfigMulti,
    DataConfigSingle,
    EmbeddingConfig,
    GovernanceConfig,
    IngestionManifest,
    ArtifactRegistryQueryConfig,
    PurviewEntityUpdateConfig,
    SearchConfig,
    SourceConfig,
    UtilityManifest,
)
from data_lake.ingest.orchestrator import (
    IngestionOrchestrator,
    _deploy_search_resource,
    _get_indexer_status,
    _load_template,
    _run_indexer,
)


# ---------------------------------------------------------------------------
# Shared constants & helpers
# ---------------------------------------------------------------------------

ENV_VARS = {
    "DATA_LAKE_SEARCH_NAME": "test-search",
    "DATA_LAKE_VECTORIZER_ENDPOINT": "https://oai.test.com",
    "DEFAULT_IDENTITY_RESOURCE_ID": (
        "/subscriptions/xxx/resourceGroups/rg/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id"
    ),
}

MINIMAL_SOURCE: Dict[str, Any] = {
    "storage_account": "teststorage",
    "resource_group": "test-rg",
    "subscription_id": "00000000-0000-0000-0000-000000000000",
    "container": "testcontainer",
}

MINIMAL_GOVERNANCE: Dict[str, Any] = {
    "purview_account": "test-purview",
    "collection": "test-collection",
}

MINIMAL_DATA: Dict[str, Any] = {
    "description": "A test dataset.",
    "artifacts": [
        {"path": "file.csv", "description": "A test file."},
    ],
}

MANIFEST_DATA: Dict[str, Any] = {
    "version": "1",
    "source": MINIMAL_SOURCE,
    "governance": MINIMAL_GOVERNANCE,
    "datasets": [
        {
            "description": "A test dataset.",
            "artifacts": [
                {"path": "data.csv", "description": "Data file"},
                {"path": "subdir/nested.nc", "description": "Nested file"},
            ],
        }
    ],
}


def _make_manifest_dict(**overrides) -> Dict[str, Any]:
    """Build a minimal valid manifest dict, with optional overrides."""
    d: Dict[str, Any] = {
        "version": "1",
        "source": MINIMAL_SOURCE,
        "governance": MINIMAL_GOVERNANCE,
        "datasets": [MINIMAL_DATA],
    }
    d.update(overrides)
    return d


def _stub_env(monkeypatch) -> None:
    """Set the standard env vars needed for manifest validation."""
    for k, v in ENV_VARS.items():
        monkeypatch.setenv(k, v)


# ---------------------------------------------------------------------------
# Orchestrator fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def manifest(monkeypatch):
    """Return a valid IngestionManifest with env vars stubbed."""
    _stub_env(monkeypatch)
    return IngestionManifest(**MANIFEST_DATA)


@pytest.fixture()
def orch(manifest):
    """Orchestrator in dry-run mode (no live calls)."""
    with patch("data_lake.ingest.orchestrator.get_purview_credential"):
        o = IngestionOrchestrator(manifest, dry_run=True)
    subfolders = manifest.iter_subfolders()
    if subfolders:
        o._apply_dataset(*subfolders[0])
    return o


@pytest.fixture()
def orch_live(manifest):
    """Orchestrator with dry_run=False (for mocked live tests)."""
    with patch("data_lake.ingest.orchestrator.get_purview_credential"):
        o = IngestionOrchestrator(manifest, dry_run=False)
    subfolders = manifest.iter_subfolders()
    if subfolders:
        o._apply_dataset(*subfolders[0])
    return o


# ---------------------------------------------------------------------------
# Sync helper
# ---------------------------------------------------------------------------


def _make_sync_instance(
    search_service: str = "test-search",
    purview_account: str = "test-purview",
    azure_openai_endpoint: str = "https://oai.test.com",
    azure_openai_deployment: str = "text-embedding-3-large",
):
    """Return an ArtifactRegistrySync instance with all clients mocked."""
    with (
        patch("data_lake.sync.sync.get_search_credential"),
        patch("data_lake.sync.sync.get_purview_credential"),
        patch("data_lake.sync.sync.get_token_provider"),
        patch("data_lake.sync.sync.SearchClient"),
        patch("data_lake.sync.sync.PurviewCatalogClient"),
        patch("data_lake.sync.sync.AzureOpenAI"),
    ):
        from data_lake.sync.sync import ArtifactRegistrySync

        sync = ArtifactRegistrySync(
            search_service=search_service,
            purview_account=purview_account,
            azure_openai_endpoint=azure_openai_endpoint,
            azure_openai_embedding_deployment=azure_openai_deployment,
        )
    return sync


# ═══════════════════════════════════════════════════════════════════════════
# Manifest models
# ═══════════════════════════════════════════════════════════════════════════


class TestSourceConfig:
    @pytest.mark.unit
    def test_defaults_source_id(self):
        cfg = SourceConfig(**MINIMAL_SOURCE)
        assert cfg.source_id == "teststorage-testcontainer"

    @pytest.mark.unit
    def test_explicit_source_id(self):
        cfg = SourceConfig(**{**MINIMAL_SOURCE, "source_id": "custom-id"})
        assert cfg.source_id == "custom-id"

    @pytest.mark.unit
    def test_managed_identity_from_env(self, monkeypatch):
        monkeypatch.setenv("DEFAULT_IDENTITY_RESOURCE_ID", "/subscriptions/xxx/mi")
        cfg = SourceConfig(**MINIMAL_SOURCE)
        assert cfg.managed_identity_id == "/subscriptions/xxx/mi"

    @pytest.mark.unit
    def test_managed_identity_explicit(self, monkeypatch):
        monkeypatch.setenv("DEFAULT_IDENTITY_RESOURCE_ID", "/subscriptions/xxx/mi")
        cfg = SourceConfig(**{**MINIMAL_SOURCE, "managed_identity_id": "/my/id"})
        assert cfg.managed_identity_id == "/my/id"

    @pytest.mark.unit
    def test_optional_extensions(self):
        cfg = SourceConfig(
            **{
                **MINIMAL_SOURCE,
                "included_extensions": [".csv", ".nc"],
                "excluded_extensions": [".zip"],
            }
        )
        assert cfg.included_extensions == [".csv", ".nc"]
        assert cfg.excluded_extensions == [".zip"]


class TestGovernanceConfig:
    @pytest.mark.unit
    def test_valid(self):
        cfg = GovernanceConfig(**MINIMAL_GOVERNANCE)
        assert cfg.purview_account == "test-purview"
        assert cfg.collection == "test-collection"

    @pytest.mark.unit
    def test_collection_is_optional(self):
        cfg = GovernanceConfig(purview_account="test-purview")  # type: ignore[call-arg]
        assert cfg.collection is None


class TestDataConfigSingle:
    @pytest.mark.unit
    def test_valid_with_artifacts(self):
        cfg = DataConfigSingle(**MINIMAL_DATA)
        assert cfg.description == "A test dataset."
        assert len(cfg.artifacts) == 1
        assert cfg.artifacts[0].path == "file.csv"

    @pytest.mark.unit
    def test_empty_artifacts_is_valid(self):
        cfg = DataConfigSingle(description="desc")
        assert cfg.artifacts == []

    @pytest.mark.unit
    def test_multiple_artifacts(self):
        cfg = DataConfigSingle(
            description="desc",
            artifacts=[  # type: ignore[arg-type]
                {"path": "a.csv", "description": "first"},
                {"path": "subdir/", "description": "folder"},
                {"path": "subdir/b.nc", "description": "second"},
            ],
        )
        assert len(cfg.artifacts) == 3


class TestDataConfigMulti:
    @pytest.mark.unit
    def test_valid_with_subfolder(self):
        cfg = DataConfigMulti(subfolder="whr", description="desc")  # type: ignore[call-arg]
        assert cfg.subfolder == "whr"
        assert cfg.description == "desc"
        assert cfg.collection is None
        assert cfg.artifacts == []

    @pytest.mark.unit
    def test_with_collection_override(self):
        cfg = DataConfigMulti(subfolder="whr", description="desc", collection="custom")
        assert cfg.collection == "custom"

    @pytest.mark.unit
    def test_no_subfolder_targets_root(self):
        cfg = DataConfigMulti(description="root-level data")  # type: ignore[call-arg]
        assert cfg.subfolder is None


class TestIterSubfolders:
    @pytest.mark.unit
    def test_basic(self, monkeypatch):
        _stub_env(monkeypatch)
        m = IngestionManifest(**_make_manifest_dict())
        result = m.iter_subfolders()
        assert len(result) == 1
        subfolder, data, collection = result[0]
        assert subfolder is None  # MINIMAL_DATA has no subfolder
        assert data.description == "A test dataset."
        assert collection == "test-collection"

    @pytest.mark.unit
    def test_per_dataset_collection_overrides_governance(self, monkeypatch):
        _stub_env(monkeypatch)
        m = IngestionManifest(
            **_make_manifest_dict(
                datasets=[
                    {"subfolder": "a", "description": "ds a", "collection": "col-a"},
                    {"subfolder": "b", "description": "ds b"},
                ]
            )
        )
        result = m.iter_subfolders()
        assert result[0][2] == "col-a"
        assert result[1][2] == "test-collection"  # falls back to governance

    @pytest.mark.unit
    def test_raises_when_no_collection(self, monkeypatch):
        _stub_env(monkeypatch)
        m = IngestionManifest(
            **_make_manifest_dict(
                governance={"purview_account": "p"},  # no collection
                datasets=[{"subfolder": "x", "description": "d"}],
            )
        )
        with pytest.raises(ValueError, match="no collection"):
            m.iter_subfolders()


class TestSearchConfig:
    @pytest.mark.unit
    def test_from_env_vars(self, monkeypatch):
        monkeypatch.setenv("DATA_LAKE_SEARCH_NAME", "my-search")
        monkeypatch.setenv("DATA_LAKE_BLOB_DETAILS_INDEX", "my-blob-idx")
        monkeypatch.setenv("DATA_LAKE_CATALOG_INDEX_NAME", "my-catalog-idx")
        cfg = SearchConfig()  # type: ignore[call-arg]
        assert cfg.search_service == "my-search"
        assert cfg.blob_details_index == "my-blob-idx"
        assert cfg.artifact_registry_index == "my-catalog-idx"

    @pytest.mark.unit
    def test_defaults_for_index_names(self, monkeypatch):
        monkeypatch.setenv("DATA_LAKE_SEARCH_NAME", "my-search")
        monkeypatch.delenv("DATA_LAKE_BLOB_DETAILS_INDEX", raising=False)
        monkeypatch.delenv("DATA_LAKE_CATALOG_INDEX_NAME", raising=False)
        cfg = SearchConfig()  # type: ignore[call-arg]
        assert cfg.blob_details_index == "blob-details"
        assert cfg.artifact_registry_index == "artifact-registry"

    @pytest.mark.unit
    def test_explicit_values_override_env(self, monkeypatch):
        monkeypatch.setenv("DATA_LAKE_SEARCH_NAME", "env-search")
        cfg = SearchConfig(search_service="explicit-search")  # type: ignore[call-arg]
        assert cfg.search_service == "explicit-search"

    @pytest.mark.unit
    def test_missing_search_service_raises(self, monkeypatch):
        monkeypatch.delenv("DATA_LAKE_SEARCH_NAME", raising=False)
        with pytest.raises(ValueError, match="search_service is required"):
            SearchConfig()  # type: ignore[call-arg]


class TestEmbeddingConfig:
    @pytest.mark.unit
    def test_from_env(self, monkeypatch):
        monkeypatch.setenv("DATA_LAKE_VECTORIZER_ENDPOINT", "https://oai.test.com")
        monkeypatch.setenv("DATA_LAKE_VECTORIZER_DEPLOYMENT", "custom-model")
        cfg = EmbeddingConfig()  # type: ignore[call-arg]
        assert cfg.azure_openai_endpoint == "https://oai.test.com"
        assert cfg.azure_openai_deployment == "custom-model"

    @pytest.mark.unit
    def test_defaults(self, monkeypatch):
        monkeypatch.delenv("DATA_LAKE_VECTORIZER_DEPLOYMENT", raising=False)
        monkeypatch.delenv("DATA_LAKE_VECTORIZER_ENDPOINT", raising=False)
        cfg = EmbeddingConfig()  # type: ignore[call-arg]
        assert cfg.azure_openai_deployment == "text-embedding-3-large"
        assert cfg.azure_openai_endpoint is None


class TestIngestionManifest:
    @pytest.mark.unit
    def test_minimal_manifest(self, monkeypatch):
        _stub_env(monkeypatch)
        m = IngestionManifest(**_make_manifest_dict())
        assert m.version == "1"
        assert m.source.source_id == "teststorage-testcontainer"
        assert m.governance.collection == "test-collection"
        assert m.search.search_service == "test-search"

    @pytest.mark.unit
    def test_full_manifest(self, monkeypatch):
        monkeypatch.delenv("DATA_LAKE_SEARCH_NAME", raising=False)
        m = IngestionManifest(
            **_make_manifest_dict(
                search={"search_service": "my-search"},
                embedding={
                    "azure_openai_endpoint": "https://oai.test.com",
                    "azure_openai_deployment": "ada-002",
                },
            )
        )
        assert m.search.search_service == "my-search"
        assert m.embedding.azure_openai_deployment == "ada-002"

    @pytest.mark.unit
    def test_missing_search_env_raises(self, monkeypatch):
        monkeypatch.delenv("DATA_LAKE_SEARCH_NAME", raising=False)
        with pytest.raises(ValueError, match="search_service is required"):
            IngestionManifest(**_make_manifest_dict())

    @pytest.mark.unit
    def test_from_yaml_roundtrip(self, tmp_path, monkeypatch):
        _stub_env(monkeypatch)
        yaml_path = tmp_path / "manifest.yaml"
        with open(yaml_path, "w") as f:
            yaml.dump(_make_manifest_dict(), f)

        m1 = IngestionManifest.from_yaml(yaml_path)
        out_path = tmp_path / "out.yaml"
        m1.to_yaml(out_path)
        m2 = IngestionManifest.from_yaml(out_path)

        assert m1.source.storage_account == m2.source.storage_account
        assert m1.governance.collection == m2.governance.collection
        assert m1.datasets[0].description == m2.datasets[0].description
        assert len(m1.datasets[0].artifacts) == len(m2.datasets[0].artifacts)

    @pytest.mark.unit
    def test_example_manifest_is_valid(self, monkeypatch):
        _stub_env(monkeypatch)
        manifest_path = Path(__file__).resolve().parent.parent / "ingest" / "example_manifest.yaml"
        if manifest_path.exists():
            m = IngestionManifest.from_yaml(manifest_path)
            assert m.source.storage_account == "grid0eastus2"
            assert m.governance.collection == "datacenter"
            assert m.datasets[0].description.strip().startswith("Datacenter")


# ═══════════════════════════════════════════════════════════════════════════
# Orchestrator – module-level helpers
# ═══════════════════════════════════════════════════════════════════════════


class TestLoadTemplate:
    @pytest.mark.unit
    def test_renders_jinja(self, tmp_path):
        tpl = tmp_path / "test.jinja"
        tpl.write_text('{"name": "{{ NAME }}", "count": {{ COUNT }}}')
        result = _load_template(tpl, {"NAME": "hello", "COUNT": "42"})
        assert result == {"name": "hello", "count": 42}


class TestDeploySearchResource:
    @pytest.mark.unit
    @patch("data_lake.ingest.orchestrator.httpx.put")
    def test_success(self, mock_put):
        mock_put.return_value = MagicMock(status_code=201)
        _deploy_search_resource(
            endpoint="https://test.search.windows.net",
            resource_type="indexers",
            resource_name="my-indexer",
            payload={"name": "my-indexer"},
            token="fake-token",
        )
        mock_put.assert_called_once()

    @pytest.mark.unit
    @patch("data_lake.ingest.orchestrator.httpx.put")
    def test_failure_raises(self, mock_put):
        resp = MagicMock(status_code=400, text="Bad request")
        resp.raise_for_status.side_effect = Exception("400 Bad Request")
        mock_put.return_value = resp
        with pytest.raises(Exception, match="400"):
            _deploy_search_resource(
                endpoint="https://test.search.windows.net",
                resource_type="indexers",
                resource_name="bad",
                payload={},
                token="fake",
            )


class TestRunIndexer:
    @pytest.mark.unit
    @patch("data_lake.ingest.orchestrator.httpx.post")
    def test_success_202(self, mock_post):
        mock_post.return_value = MagicMock(status_code=202)
        _run_indexer("https://test.search.windows.net", "idx", "tok")
        mock_post.assert_called_once()


class TestGetIndexerStatus:
    @pytest.mark.unit
    @patch("data_lake.ingest.orchestrator.httpx.get")
    def test_returns_json(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"lastResult": {"status": "success", "itemCount": 10}},
        )
        mock_get.return_value.raise_for_status = MagicMock()
        result = _get_indexer_status("https://test.search.windows.net", "idx", "tok")
        assert result["lastResult"]["status"] == "success"


# ═══════════════════════════════════════════════════════════════════════════
# Orchestrator – dry-run
# ═══════════════════════════════════════════════════════════════════════════


class TestOrchestratorDryRun:
    @pytest.mark.unit
    def test_step3_dry_run(self, orch, caplog):
        with caplog.at_level(logging.INFO):
            orch.step3_register_and_create_scan()
        assert "DRY RUN" in caplog.text
        assert "teststorage" in caplog.text

    @pytest.mark.unit
    def test_step4a_dry_run(self, orch, caplog):
        with caplog.at_level(logging.INFO):
            orch.step4a_trigger_purview_scan()
        assert "DRY RUN" in caplog.text

    @pytest.mark.unit
    def test_step4b_dry_run(self, orch, caplog):
        with patch("data_lake.ingest.orchestrator._get_token", return_value="fake-token"):
            with caplog.at_level(logging.INFO):
                indexer_name, prev_start = orch.step4b_create_and_run_indexer()
        assert "DRY RUN" in caplog.text
        assert "blob-details-indexer-" in indexer_name
        assert prev_start is None

    @pytest.mark.unit
    def test_wait_for_indexer_dry_run(self, orch, caplog):
        with caplog.at_level(logging.INFO):
            orch.wait_for_indexer("some-indexer")
        assert "DRY RUN" in caplog.text

    @pytest.mark.unit
    def test_wait_for_purview_scan_dry_run(self, orch, caplog):
        with caplog.at_level(logging.INFO):
            orch.wait_for_purview_scan("some-scan")
        assert "DRY RUN" in caplog.text

    @pytest.mark.unit
    def test_step5_dry_run(self, orch, caplog):
        with caplog.at_level(logging.INFO):
            orch.step5_push_descriptions()
        assert "DRY RUN" in caplog.text
        assert caplog.text.count("DRY RUN") >= 3

    @pytest.mark.unit
    def test_step6_dry_run(self, orch, caplog):
        with caplog.at_level(logging.INFO):
            orch.step6_sync_registry()
        assert "DRY RUN" in caplog.text

    @pytest.mark.unit
    def test_run_dry_run(self, orch, caplog):
        with patch("data_lake.ingest.orchestrator._get_token", return_value="fake-token"):
            with caplog.at_level(logging.INFO):
                orch.run()
        assert "INGESTION PIPELINE COMPLETE" in caplog.text


# ═══════════════════════════════════════════════════════════════════════════
# Orchestrator – mocked live behaviour
# ═══════════════════════════════════════════════════════════════════════════


class TestStep4a:
    @pytest.mark.unit
    def test_active_run_exist_is_swallowed(self, orch_live, caplog):
        mock_mgr = MagicMock()
        mock_mgr.trigger_scan.side_effect = Exception(
            "ScanHistory_ActiveRunExist: There is already a scan currently running."
        )
        with (
            caplog.at_level(logging.INFO),
            patch("data_lake.semantic.PurviewDataSourceManager", return_value=mock_mgr),
        ):
            orch_live.step4a_trigger_purview_scan()
        assert "scan already running" in caplog.text

    @pytest.mark.unit
    def test_other_exception_is_raised(self, orch_live):
        mock_mgr = MagicMock()
        mock_mgr.trigger_scan.side_effect = RuntimeError("Something else broke")
        with patch("data_lake.semantic.PurviewDataSourceManager", return_value=mock_mgr):
            with pytest.raises(RuntimeError, match="Something else broke"):
                orch_live.step4a_trigger_purview_scan()


class TestStep5:
    """Test step5_push_descriptions entity-type resolution & update logic."""

    _PURVIEW_PATCH = "azure.purview.catalog.PurviewCatalogClient"

    @pytest.mark.unit
    def test_container_path_uses_blob_container_type(self, orch_live):
        mock_catalog = MagicMock()
        entity_result = {
            "entity": {
                "guid": "test-guid-123",
                "typeName": "azure_blob_container",
                "attributes": {
                    "qualifiedName": "https://teststorage.blob.core.windows.net/testcontainer",
                    "name": "testcontainer",
                },
            }
        }
        mock_catalog.entity.get_by_unique_attributes.return_value = entity_result
        mock_catalog.entity.create_or_update.return_value = MagicMock()

        with patch(self._PURVIEW_PATCH, return_value=mock_catalog):
            orch_live.step5_push_descriptions()

        calls = mock_catalog.entity.get_by_unique_attributes.call_args_list
        first_call_kwargs = calls[0][1] if calls[0][1] else {}
        first_call_type = first_call_kwargs.get("type_name") or (calls[0][0][0] if calls[0][0] else None)
        assert first_call_type == "azure_blob_container"

    @pytest.mark.unit
    def test_subpath_uses_blob_path_type(self, orch_live):
        mock_catalog = MagicMock()
        entity_result = {
            "entity": {
                "guid": "guid-456",
                "typeName": "azure_blob_path",
                "attributes": {
                    "qualifiedName": "https://teststorage.blob.core.windows.net/testcontainer/data.csv",
                    "name": "data.csv",
                },
            }
        }
        mock_catalog.entity.get_by_unique_attributes.return_value = entity_result
        mock_catalog.entity.create_or_update.return_value = MagicMock()

        with patch(self._PURVIEW_PATCH, return_value=mock_catalog):
            orch_live.step5_push_descriptions()

        calls = mock_catalog.entity.get_by_unique_attributes.call_args_list
        type_names_used = [c.kwargs.get("type_name") or c[1].get("type_name") for c in calls]
        assert "azure_blob_path" in type_names_used

    @pytest.mark.unit
    def test_update_body_includes_name_and_guid(self, orch_live):
        mock_catalog = MagicMock()
        entity_result = {
            "entity": {
                "guid": "guid-789",
                "typeName": "azure_blob_path",
                "attributes": {
                    "qualifiedName": "https://teststorage.blob.core.windows.net/testcontainer/data.csv",
                    "name": "data.csv",
                },
            }
        }
        mock_catalog.entity.get_by_unique_attributes.return_value = entity_result
        mock_catalog.entity.create_or_update.return_value = MagicMock()

        with patch(self._PURVIEW_PATCH, return_value=mock_catalog):
            orch_live.step5_push_descriptions()

        update_calls = mock_catalog.entity.create_or_update.call_args_list
        assert len(update_calls) >= 1
        for call in update_calls:
            body = call.kwargs.get("entity") or call[1].get("entity")
            entity_body = body["entity"]
            assert "guid" in entity_body
            assert entity_body["attributes"]["name"] is not None

    @pytest.mark.unit
    def test_retry_on_not_found(self, orch_live, caplog):
        from azure.core.exceptions import ResourceNotFoundError

        mock_catalog = MagicMock()
        entity_result = {
            "entity": {
                "guid": "guid-retry",
                "typeName": "azure_blob_path",
                "attributes": {
                    "qualifiedName": "https://teststorage.blob.core.windows.net/testcontainer/data.csv",
                    "name": "data.csv",
                },
            }
        }
        mock_catalog.entity.get_by_unique_attributes.side_effect = [
            ResourceNotFoundError("nope"),  # attempt 1: azure_blob_container
            ResourceNotFoundError("nope"),  # attempt 1: azure_blob_path
            ResourceNotFoundError("nope"),  # attempt 2: azure_blob_container
            ResourceNotFoundError("nope"),  # attempt 2: azure_blob_path
            ResourceNotFoundError("nope"),  # attempt 3: azure_blob_container
            ResourceNotFoundError("nope"),  # attempt 3: azure_blob_path
            ResourceNotFoundError("nope"),  # attempt 4: azure_blob_container
            ResourceNotFoundError("nope"),  # attempt 4: azure_blob_path
            ResourceNotFoundError("nope"),  # attempt 5: azure_blob_container
            ResourceNotFoundError("nope"),  # attempt 5: azure_blob_path
            ResourceNotFoundError("nope"),  # attempt 6: azure_blob_container
            ResourceNotFoundError("nope"),  # attempt 6: azure_blob_path → fails
            entity_result,  # data.csv found
            entity_result,  # subdir/nested.nc found
        ]
        mock_catalog.entity.create_or_update.return_value = MagicMock()

        with (
            caplog.at_level(logging.INFO),
            patch(self._PURVIEW_PATCH, return_value=mock_catalog),
            patch("data_lake.ingest.orchestrator.time.sleep"),
        ):
            orch_live.step5_push_descriptions()

        assert "2 updated, 1 failed" in caplog.text


class TestWaitForIndexer:
    @pytest.mark.unit
    def test_returns_on_success(self, orch_live):
        with (
            patch(
                "data_lake.ingest.orchestrator._get_indexer_status",
                return_value={"lastResult": {"status": "success", "itemCount": 5}},
            ),
            patch("data_lake.ingest.orchestrator._get_token", return_value="tok"),
        ):
            orch_live.wait_for_indexer("idx", poll_interval=0, max_wait=5)

    @pytest.mark.unit
    def test_returns_on_failure(self, orch_live, caplog):
        with (
            patch(
                "data_lake.ingest.orchestrator._get_indexer_status",
                return_value={
                    "lastResult": {
                        "status": "persistentFailure",
                        "errorMessage": "boom",
                    }
                },
            ),
            patch("data_lake.ingest.orchestrator._get_token", return_value="tok"),
            caplog.at_level(logging.WARNING),
        ):
            orch_live.wait_for_indexer("idx", poll_interval=0, max_wait=5)
        assert "persistentFailure" in caplog.text

    @pytest.mark.unit
    def test_polls_until_done(self, orch_live):
        statuses = [
            {"lastResult": {"status": "inProgress"}},
            {"lastResult": {"status": "inProgress"}},
            {"lastResult": {"status": "success", "itemCount": 3}},
        ]
        with (
            patch(
                "data_lake.ingest.orchestrator._get_indexer_status",
                side_effect=statuses,
            ),
            patch("data_lake.ingest.orchestrator._get_token", return_value="tok"),
            patch("data_lake.ingest.orchestrator.time.sleep"),
        ):
            orch_live.wait_for_indexer("idx", poll_interval=1, max_wait=100)

    @pytest.mark.unit
    def test_waits_while_start_time_is_stale(self, orch_live):
        """Poller skips results whose startTime matches prev_start_time (stale),
        then proceeds once a new startTime is observed."""
        prev_start = "2026-01-01T00:00:00Z"
        new_start = "2026-01-02T00:00:00Z"

        # First two calls return the stale startTime; the third returns a new
        # startTime with a terminal status.
        statuses = [
            {"lastResult": {"status": "success", "startTime": prev_start, "itemCount": 0}},
            {"lastResult": {"status": "success", "startTime": prev_start, "itemCount": 0}},
            {"lastResult": {"status": "success", "startTime": new_start, "itemCount": 7}},
        ]
        mock_sleep = MagicMock()
        with (
            patch(
                "data_lake.ingest.orchestrator._get_indexer_status",
                side_effect=statuses,
            ) as mock_status,
            patch("data_lake.ingest.orchestrator._get_token", return_value="tok"),
            patch("data_lake.ingest.orchestrator.time.sleep", mock_sleep),
        ):
            orch_live.wait_for_indexer(
                "idx",
                prev_start_time=prev_start,
                poll_interval=1,
                max_wait=100,
            )

        # Status should have been polled exactly 3 times.
        assert mock_status.call_count == 3
        # Sleep should have been called for each of the two stale responses.
        assert mock_sleep.call_count == 2


class TestWaitForPurviewScan:
    @pytest.mark.unit
    def test_returns_on_succeeded(self, orch_live, caplog):
        mock_mgr = MagicMock()
        mock_mgr.scanning_client.scan_result.list_scan_history.return_value = iter([{"status": "Succeeded"}])
        with (
            patch("data_lake.semantic.PurviewDataSourceManager", return_value=mock_mgr),
            caplog.at_level(logging.INFO),
        ):
            orch_live.wait_for_purview_scan("scan-1", poll_interval=0, max_wait=5)
        assert "scan succeeded" in caplog.text

    @pytest.mark.unit
    def test_polls_until_succeeded(self, orch_live):
        mock_mgr = MagicMock()
        call_count = 0

        def _history(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return iter([{"status": "InProgress"}])
            return iter([{"status": "Succeeded"}])

        mock_mgr.scanning_client.scan_result.list_scan_history.side_effect = _history
        with (
            patch("data_lake.semantic.PurviewDataSourceManager", return_value=mock_mgr),
            patch("data_lake.ingest.orchestrator.time.sleep"),
        ):
            orch_live.wait_for_purview_scan("scan-1", poll_interval=1, max_wait=100)
        assert call_count == 3


class TestRunFullPipeline:
    @pytest.mark.unit
    def test_run_calls_all_steps(self, orch_live, caplog):
        with (
            patch.object(orch_live, "step3_register_and_create_scan") as s3,
            patch.object(orch_live, "step4a_trigger_purview_scan") as s4a,
            patch.object(
                orch_live, "step4b_create_and_run_indexer", return_value=("idx", "2026-01-01T00:00:00Z")
            ) as s4b,
            patch.object(orch_live, "wait_for_indexer") as wi,
            patch.object(orch_live, "wait_for_purview_scan") as wps,
            patch.object(orch_live, "step5_push_descriptions") as s5,
            patch.object(orch_live, "step6_sync_registry") as s6,
            caplog.at_level(logging.INFO),
        ):
            orch_live.run()

        s3.assert_called_once()
        s4a.assert_called_once()
        s4b.assert_called_once()
        wi.assert_called_once_with("idx", prev_start_time="2026-01-01T00:00:00Z")
        wps.assert_called_once()
        s5.assert_called_once()
        s6.assert_called_once()
        assert "INGESTION PIPELINE COMPLETE" in caplog.text


# ═══════════════════════════════════════════════════════════════════════════
# Artifact registry sync
# ═══════════════════════════════════════════════════════════════════════════


class TestGetPurviewEntity:
    @pytest.mark.unit
    def test_returns_entity(self):
        sync = _make_sync_instance()
        entity = {"entity": {"attributes": {"qualifiedName": "https://a/b"}}}
        sync.catalog_client.entity.get_by_unique_attributes.return_value = entity
        result = sync.get_purview_entity("https://a/b")
        assert result is not None
        assert result["entity"]["attributes"]["qualifiedName"] == "https://a/b"

    @pytest.mark.unit
    def test_caches_result(self):
        sync = _make_sync_instance()
        entity = {"entity": {"attributes": {"qualifiedName": "https://a/b"}}}
        sync.catalog_client.entity.get_by_unique_attributes.return_value = entity
        sync.get_purview_entity("https://a/b")
        sync.get_purview_entity("https://a/b")
        assert sync.catalog_client.entity.get_by_unique_attributes.call_count == 1

    @pytest.mark.unit
    def test_returns_none_on_not_found(self):
        from azure.core.exceptions import ResourceNotFoundError

        sync = _make_sync_instance()
        sync.catalog_client.entity.get_by_unique_attributes.side_effect = ResourceNotFoundError("nope")
        assert sync.get_purview_entity("https://missing") is None

    @pytest.mark.unit
    def test_raises_purview_lookup_error_on_general_error(self):
        from data_lake.sync.sync import PurviewLookupError

        sync = _make_sync_instance()
        sync.catalog_client.entity.get_by_unique_attributes.side_effect = Exception("boom")
        with pytest.raises(PurviewLookupError):
            sync.get_purview_entity("https://err")


class TestFindSemanticParent:
    @pytest.mark.unit
    def test_finds_parent_folder_with_description(self):
        sync = _make_sync_instance()
        blob_entity = {
            "entity": {"attributes": {"qualifiedName": "https://acct.blob.core.windows.net/ctr/folder/file.csv"}}
        }
        parent_entity = {
            "entity": {
                "typeName": "azure_blob_path",
                "attributes": {
                    "qualifiedName": "https://acct.blob.core.windows.net/ctr/folder/",
                    "name": "folder",
                    "userDescription": "Folder with data",
                },
            }
        }
        sync.catalog_client.entity.get_by_unique_attributes.return_value = parent_entity
        result = sync.find_semantic_parent(blob_entity)
        assert result is not None
        assert result["entity"]["attributes"]["name"] == "folder"

    @pytest.mark.unit
    def test_finds_container_with_azure_blob_container_type(self):
        sync = _make_sync_instance()
        blob_entity = {"entity": {"attributes": {"qualifiedName": "https://acct.blob.core.windows.net/ctr/file.csv"}}}
        from azure.core.exceptions import ResourceNotFoundError

        container_entity = {
            "entity": {
                "typeName": "azure_blob_container",
                "attributes": {
                    "qualifiedName": "https://acct.blob.core.windows.net/ctr",
                    "name": "ctr",
                    "userDescription": "Container description",
                },
            }
        }

        def _mock_get(qualified_name=None, type_name=None, **kw):
            qn = kw.get("attr_qualified_name", qualified_name)
            tn = type_name
            if tn == "azure_blob_container" and qn == "https://acct.blob.core.windows.net/ctr":
                return container_entity
            raise ResourceNotFoundError("nope")

        sync.catalog_client.entity.get_by_unique_attributes.side_effect = _mock_get
        result = sync.find_semantic_parent(blob_entity)
        assert result is not None
        assert result["entity"]["typeName"] == "azure_blob_container"

    @pytest.mark.unit
    def test_returns_none_when_no_parent_found(self):
        sync = _make_sync_instance()
        blob_entity = {"entity": {"attributes": {"qualifiedName": "https://acct.blob.core.windows.net/ctr/file.csv"}}}
        no_desc_entity = {
            "entity": {
                "typeName": "azure_blob_path",
                "attributes": {
                    "qualifiedName": "https://acct.blob.core.windows.net/ctr/",
                    "name": "ctr",
                },
            }
        }
        sync.catalog_client.entity.get_by_unique_attributes.return_value = no_desc_entity
        assert sync.find_semantic_parent(blob_entity) is None

    @pytest.mark.unit
    def test_container_qualified_name_no_trailing_slash(self):
        sync = _make_sync_instance()
        blob_entity = {"entity": {"attributes": {"qualifiedName": "https://acct.blob.core.windows.net/ctr/file.csv"}}}
        from azure.core.exceptions import ResourceNotFoundError

        calls = []

        def _mock_get(**kw):
            calls.append(kw)
            raise ResourceNotFoundError("nope")

        sync.catalog_client.entity.get_by_unique_attributes.side_effect = _mock_get
        sync.find_semantic_parent(blob_entity)

        container_calls = [c for c in calls if c.get("type_name") == "azure_blob_container"]
        assert len(container_calls) >= 1
        for c in container_calls:
            assert not c["attr_qualified_name"].endswith("/")


class TestStripHtml:
    @pytest.mark.unit
    def test_strips_tags(self):
        sync = _make_sync_instance()
        assert sync.strip_html("<p>Hello <b>world</b></p>") == "Hello world"

    @pytest.mark.unit
    def test_returns_none_for_none_or_empty(self):
        sync = _make_sync_instance()
        assert sync.strip_html(None) is None
        assert sync.strip_html("") is None

    @pytest.mark.unit
    def test_collapses_whitespace(self):
        sync = _make_sync_instance()
        assert sync.strip_html("<p>  foo   bar  </p>") == "foo bar"


class TestGetCollectionDomain:
    @pytest.mark.unit
    def test_extracts_from_dict(self):
        sync = _make_sync_instance()
        entity = {"entity": {"attributes": {"collection": {"referenceName": "my-collection"}}}}
        assert sync.get_collection_domain(entity) == "my-collection"

    @pytest.mark.unit
    def test_extracts_from_string(self):
        sync = _make_sync_instance()
        entity = {"entity": {"attributes": {"collection": "simple-name"}}}
        assert sync.get_collection_domain(entity) == "simple-name"

    @pytest.mark.unit
    def test_returns_none_on_missing(self):
        sync = _make_sync_instance()
        assert sync.get_collection_domain({"entity": {"attributes": {}}}) is None


class TestGenerateEmbedding:
    @pytest.mark.unit
    def test_returns_vector(self):
        sync = _make_sync_instance()
        fake_vector = [0.1, 0.2, 0.3]
        mock_resp = MagicMock()
        mock_resp.data = [MagicMock(embedding=fake_vector)]
        sync.openai_client.embeddings.create.return_value = mock_resp
        assert sync.generate_embedding("hello world") == fake_vector

    @pytest.mark.unit
    def test_raises_on_empty_text(self):
        sync = _make_sync_instance()
        with pytest.raises(ValueError, match="empty"):
            sync.generate_embedding("")


class TestEnrichArtifact:
    @pytest.mark.unit
    def test_raises_value_error_for_missing_fields(self):
        sync = _make_sync_instance()
        with pytest.raises(ValueError, match="missing required fields"):
            sync.enrich_artifact({})
        with pytest.raises(ValueError, match="missing required fields"):
            sync.enrich_artifact({"metadata_storage_path": "x"})
        with pytest.raises(ValueError, match="missing required fields"):
            sync.enrich_artifact({"artifact_id": "x"})

    @pytest.mark.unit
    def test_raises_stale_artifact_error_when_purview_entity_not_found(self):
        from azure.core.exceptions import ResourceNotFoundError

        from data_lake.sync.sync import StaleArtifactError

        sync = _make_sync_instance()
        sync.catalog_client.entity.get_by_unique_attributes.side_effect = ResourceNotFoundError("Entity not found")
        with pytest.raises(StaleArtifactError):
            sync.enrich_artifact(
                {
                    "metadata_storage_path": "https://a.blob.core.windows.net/c/f.csv",
                    "artifact_id": "abc123",
                }
            )

    @pytest.mark.unit
    def test_raises_purview_lookup_error_on_transient_failure(self):
        from data_lake.sync.sync import PurviewLookupError

        sync = _make_sync_instance()
        sync.catalog_client.entity.get_by_unique_attributes.side_effect = Exception("connection timeout")
        with pytest.raises(PurviewLookupError):
            sync.enrich_artifact(
                {
                    "metadata_storage_path": "https://a.blob.core.windows.net/c/f.csv",
                    "artifact_id": "abc123",
                }
            )


class TestSyncArtifacts:
    @pytest.mark.unit
    def test_continues_on_enrich_exception(self):
        sync = _make_sync_instance()
        blob1 = {
            "metadata_storage_path": "https://a.blob.core.windows.net/c/bad.csv",
            "artifact_id": "bad",
        }
        blob2 = {
            "metadata_storage_path": "https://a.blob.core.windows.net/c/good.csv",
            "artifact_id": "good",
        }
        sync.blob_details_client.search.return_value = iter([blob1, blob2])

        def _mock_enrich(blob):
            if blob["artifact_id"] == "bad":
                raise ValueError("missing description")
            return {"artifact_id": "good", "name": "good.csv"}

        sync.enrich_artifact = _mock_enrich
        sync._upload_batch = MagicMock(return_value=(1, 0))

        stats = sync.sync_artifacts(dry_run=False)
        assert stats["processed"] == 2
        assert stats["failed"] >= 1
        assert stats["enriched"] >= 1

    @pytest.mark.unit
    def test_stale_artifact_increments_skipped_not_failed(self):
        from data_lake.sync.sync import StaleArtifactError

        sync = _make_sync_instance()
        blob = {
            "metadata_storage_path": "https://a.blob.core.windows.net/c/stale.csv",
            "artifact_id": "stale",
        }
        sync.blob_details_client.search.return_value = iter([blob])

        def _mock_enrich(_blob):
            raise StaleArtifactError("Purview entity not found")

        sync.enrich_artifact = _mock_enrich

        stats = sync.sync_artifacts(dry_run=False)
        assert stats["processed"] == 1
        assert stats["skipped"] == 1
        assert stats["failed"] == 0

    @pytest.mark.unit
    def test_cleanup_deletes_stale_entries(self):
        from data_lake.sync.sync import StaleArtifactError

        sync = _make_sync_instance()
        blob = {
            "metadata_storage_path": "https://a.blob.core.windows.net/c/stale.csv",
            "artifact_id": "stale",
        }
        sync.blob_details_client.search.return_value = iter([blob])

        def _mock_enrich(_blob):
            raise StaleArtifactError("Purview entity not found")

        sync.enrich_artifact = _mock_enrich
        sync._delete_stale_entries = MagicMock(return_value=True)

        stats = sync.sync_artifacts(dry_run=False, cleanup=True)
        assert stats["processed"] == 1
        assert stats["cleaned"] == 1
        assert stats["skipped"] == 0
        assert stats["failed"] == 0
        sync._delete_stale_entries.assert_called_once_with("stale")

    @pytest.mark.unit
    def test_cleanup_only_deletes_from_artifact_registry(self):
        """Verify _delete_stale_entries does NOT touch blob-details (source of truth)."""
        sync = _make_sync_instance()
        # Replace with distinct mocks so we can verify which is called
        mock_blob = MagicMock()
        mock_artifact = MagicMock()
        sync.blob_details_client = mock_blob
        sync.artifact_registry_client = mock_artifact

        sync._delete_stale_entries("some-id")

        mock_artifact.delete_documents.assert_called_once_with(documents=[{"artifact_id": "some-id"}])
        mock_blob.delete_documents.assert_not_called()

    @pytest.mark.unit
    def test_purview_lookup_error_increments_failed_not_cleaned(self):
        from data_lake.sync.sync import PurviewLookupError

        sync = _make_sync_instance()
        blob = {
            "metadata_storage_path": "https://a.blob.core.windows.net/c/err.csv",
            "artifact_id": "err",
        }
        sync.blob_details_client.search.return_value = iter([blob])

        def _mock_enrich(_blob):
            raise PurviewLookupError("connection timeout")

        sync.enrich_artifact = _mock_enrich
        sync._delete_stale_entries = MagicMock()

        stats = sync.sync_artifacts(dry_run=False, cleanup=True)
        assert stats["failed"] == 1
        assert stats["cleaned"] == 0
        sync._delete_stale_entries.assert_not_called()

    @pytest.mark.unit
    def test_max_cleanup_cap(self):
        from data_lake.sync.sync import StaleArtifactError

        sync = _make_sync_instance()
        blobs = [
            {
                "metadata_storage_path": f"https://a.blob.core.windows.net/c/{i}.csv",
                "artifact_id": f"stale-{i}",
            }
            for i in range(5)
        ]
        sync.blob_details_client.search.return_value = iter(blobs)
        sync.enrich_artifact = MagicMock(side_effect=StaleArtifactError("gone"))
        sync._delete_stale_entries = MagicMock(return_value=True)

        stats = sync.sync_artifacts(dry_run=False, cleanup=True, max_cleanup=2, cleanup_threshold=1.0)
        assert stats["cleaned"] == 2
        assert stats["skipped"] == 3
        assert sync._delete_stale_entries.call_count == 2

    @pytest.mark.unit
    def test_circuit_breaker_aborts_on_high_stale_ratio(self):
        from data_lake.sync.sync import StaleArtifactError

        sync = _make_sync_instance()
        # 12 blobs, all stale — should trip circuit breaker after 10 processed
        blobs = [
            {
                "metadata_storage_path": f"https://a.blob.core.windows.net/c/{i}.csv",
                "artifact_id": f"stale-{i}",
            }
            for i in range(12)
        ]
        sync.blob_details_client.search.return_value = iter(blobs)
        sync.enrich_artifact = MagicMock(side_effect=StaleArtifactError("gone"))
        sync._delete_stale_entries = MagicMock(return_value=True)

        stats = sync.sync_artifacts(
            dry_run=False,
            cleanup=True,
            max_cleanup=100,
            cleanup_threshold=0.2,
        )
        # Circuit breaker should stop processing before all 12 are cleaned
        assert stats["cleaned"] < 12
        assert stats["processed"] <= 12

    @pytest.mark.unit
    def test_cleanup_skipped_in_dry_run(self):
        from data_lake.sync.sync import StaleArtifactError

        sync = _make_sync_instance()
        blob = {
            "metadata_storage_path": "https://a.blob.core.windows.net/c/stale.csv",
            "artifact_id": "stale",
        }
        sync.blob_details_client.search.return_value = iter([blob])

        def _mock_enrich(_blob):
            raise StaleArtifactError("Purview entity not found")

        sync.enrich_artifact = _mock_enrich
        sync._delete_stale_entries = MagicMock()

        stats = sync.sync_artifacts(dry_run=True, cleanup=True)
        # cleanup=True but dry_run=True, so no deletion
        sync._delete_stale_entries.assert_not_called()
        assert stats["skipped"] == 1

    @pytest.mark.unit
    def test_verify_blobs_404_with_cleanup_deletes_and_increments_cleaned(self):
        sync = _make_sync_instance()
        blob = {
            "metadata_storage_path": "https://a.blob.core.windows.net/c/gone.csv",
            "artifact_id": "gone",
        }
        sync.blob_details_client.search.return_value = iter([blob])
        sync._blob_exists = MagicMock(return_value=False)
        sync._delete_stale_entries = MagicMock(return_value=True)
        sync.enrich_artifact = MagicMock()

        stats = sync.sync_artifacts(dry_run=False, verify_blobs=True, cleanup=True)
        assert stats["processed"] == 1
        assert stats["cleaned"] == 1
        assert stats["skipped"] == 0
        assert stats["failed"] == 0
        sync._blob_exists.assert_called_once_with("https://a.blob.core.windows.net/c/gone.csv")
        sync._delete_stale_entries.assert_called_once_with("gone")
        sync.enrich_artifact.assert_not_called()

    @pytest.mark.unit
    def test_verify_blobs_404_without_cleanup_increments_skipped(self):
        sync = _make_sync_instance()
        blob = {
            "metadata_storage_path": "https://a.blob.core.windows.net/c/gone.csv",
            "artifact_id": "gone",
        }
        sync.blob_details_client.search.return_value = iter([blob])
        sync._blob_exists = MagicMock(return_value=False)
        sync._delete_stale_entries = MagicMock()
        sync.enrich_artifact = MagicMock()

        stats = sync.sync_artifacts(dry_run=False, verify_blobs=True, cleanup=False)
        assert stats["processed"] == 1
        assert stats["skipped"] == 1
        assert stats["cleaned"] == 0
        assert stats["failed"] == 0
        sync._delete_stale_entries.assert_not_called()
        sync.enrich_artifact.assert_not_called()

    @pytest.mark.unit
    def test_verify_blobs_404_delete_failure_increments_failed(self):
        sync = _make_sync_instance()
        blob = {
            "metadata_storage_path": "https://a.blob.core.windows.net/c/gone.csv",
            "artifact_id": "gone",
        }
        sync.blob_details_client.search.return_value = iter([blob])
        sync._blob_exists = MagicMock(return_value=False)
        sync._delete_stale_entries = MagicMock(return_value=False)

        stats = sync.sync_artifacts(dry_run=False, verify_blobs=True, cleanup=True)
        assert stats["processed"] == 1
        assert stats["failed"] == 1
        assert stats["cleaned"] == 0

    @pytest.mark.unit
    def test_verify_blobs_404_dry_run_skips_without_deleting(self):
        sync = _make_sync_instance()
        blob = {
            "metadata_storage_path": "https://a.blob.core.windows.net/c/gone.csv",
            "artifact_id": "gone",
        }
        sync.blob_details_client.search.return_value = iter([blob])
        sync._blob_exists = MagicMock(return_value=False)
        sync._delete_stale_entries = MagicMock()

        stats = sync.sync_artifacts(dry_run=True, verify_blobs=True, cleanup=True)
        assert stats["skipped"] == 1
        assert stats["cleaned"] == 0
        sync._delete_stale_entries.assert_not_called()

    @pytest.mark.unit
    def test_verify_blobs_exists_proceeds_to_enrich(self):
        sync = _make_sync_instance()
        blob = {
            "metadata_storage_path": "https://a.blob.core.windows.net/c/ok.csv",
            "artifact_id": "ok",
        }
        sync.blob_details_client.search.return_value = iter([blob])
        sync._blob_exists = MagicMock(return_value=True)
        sync.enrich_artifact = MagicMock(return_value={"artifact_id": "ok", "name": "ok.csv"})
        sync._upload_batch = MagicMock(return_value=(1, 0))

        stats = sync.sync_artifacts(dry_run=False, verify_blobs=True)
        assert stats["enriched"] == 1
        sync.enrich_artifact.assert_called_once()

    @pytest.mark.unit
    def test_verify_blobs_404_respects_max_cleanup_cap(self):
        sync = _make_sync_instance()
        blobs = [
            {
                "metadata_storage_path": f"https://a.blob.core.windows.net/c/{i}.csv",
                "artifact_id": f"gone-{i}",
            }
            for i in range(5)
        ]
        sync.blob_details_client.search.return_value = iter(blobs)
        sync._blob_exists = MagicMock(return_value=False)
        sync._delete_stale_entries = MagicMock(return_value=True)

        stats = sync.sync_artifacts(
            dry_run=False,
            verify_blobs=True,
            cleanup=True,
            max_cleanup=2,
            cleanup_threshold=1.0,
        )
        assert stats["cleaned"] == 2
        assert stats["skipped"] == 3
        assert sync._delete_stale_entries.call_count == 2


# ═══════════════════════════════════════════════════════════════════════════
# Utilities – update_purview_entity
# ═══════════════════════════════════════════════════════════════════════════


_CATALOG_PATCH = "data_lake.utilities.utilities.PurviewCatalogClient"
_PURVIEW_CRED_PATCH = "data_lake.utilities.utilities.get_purview_credential"
_SEARCH_CRED_PATCH = "data_lake.utilities.utilities.get_search_credential"


def _make_entity_result(
    guid: str = "test-guid",
    type_name: str = "azure_blob_path",
    name: str = "file.csv",
    qualified_name: str = "https://acct.blob.core.windows.net/ctr/file.csv",
    user_description: str = "original description",
) -> dict:
    return {
        "entity": {
            "guid": guid,
            "typeName": type_name,
            "attributes": {
                "qualifiedName": qualified_name,
                "name": name,
                "userDescription": user_description,
            },
        }
    }


class TestUpdatePurviewEntity:
    """Tests for data_lake.utilities.utilities.update_purview_entity."""

    @pytest.mark.unit
    def test_raises_when_no_updates_provided(self):
        from data_lake.utilities.utilities import update_purview_entity

        with pytest.raises(ValueError, match="At least one"):
            update_purview_entity("purview", "https://acct.blob.core.windows.net/ctr/file.csv")

    @pytest.mark.unit
    def test_updates_name_only(self):
        from data_lake.utilities.utilities import update_purview_entity

        mock_catalog = MagicMock()
        mock_catalog.entity.get_by_unique_attributes.return_value = _make_entity_result()
        mock_catalog.entity.create_or_update.return_value = MagicMock()

        with (
            patch(_PURVIEW_CRED_PATCH),
            patch(_CATALOG_PATCH, return_value=mock_catalog),
        ):
            update_purview_entity(
                "purview",
                "https://acct.blob.core.windows.net/ctr/file.csv",
                new_name="renamed.csv",
            )

        call = mock_catalog.entity.create_or_update.call_args
        body = call.kwargs.get("entity") or call[1].get("entity")
        assert body["entity"]["attributes"]["name"] == "renamed.csv"
        assert body["entity"]["attributes"]["userDescription"] == "original description"

    @pytest.mark.unit
    def test_updates_description_only(self):
        from data_lake.utilities.utilities import update_purview_entity

        mock_catalog = MagicMock()
        mock_catalog.entity.get_by_unique_attributes.return_value = _make_entity_result()
        mock_catalog.entity.create_or_update.return_value = MagicMock()

        with (
            patch(_PURVIEW_CRED_PATCH),
            patch(_CATALOG_PATCH, return_value=mock_catalog),
        ):
            update_purview_entity(
                "purview",
                "https://acct.blob.core.windows.net/ctr/file.csv",
                new_description="brand new description",
            )

        call = mock_catalog.entity.create_or_update.call_args
        body = call.kwargs.get("entity") or call[1].get("entity")
        assert body["entity"]["attributes"]["userDescription"] == "brand new description"
        assert body["entity"]["attributes"]["name"] == "file.csv"

    @pytest.mark.unit
    def test_updates_both_name_and_description(self):
        from data_lake.utilities.utilities import update_purview_entity

        mock_catalog = MagicMock()
        mock_catalog.entity.get_by_unique_attributes.return_value = _make_entity_result()
        mock_catalog.entity.create_or_update.return_value = MagicMock()

        with (
            patch(_PURVIEW_CRED_PATCH),
            patch(_CATALOG_PATCH, return_value=mock_catalog),
        ):
            update_purview_entity(
                "purview",
                "https://acct.blob.core.windows.net/ctr/file.csv",
                new_name="new.csv",
                new_description="new desc",
            )

        call = mock_catalog.entity.create_or_update.call_args
        body = call.kwargs.get("entity") or call[1].get("entity")
        assert body["entity"]["attributes"]["name"] == "new.csv"
        assert body["entity"]["attributes"]["userDescription"] == "new desc"

    @pytest.mark.unit
    def test_dry_run_does_not_call_create_or_update(self):
        from data_lake.utilities.utilities import update_purview_entity

        mock_catalog = MagicMock()
        mock_catalog.entity.get_by_unique_attributes.return_value = _make_entity_result()

        with (
            patch(_PURVIEW_CRED_PATCH),
            patch(_CATALOG_PATCH, return_value=mock_catalog),
        ):
            update_purview_entity(
                "purview",
                "https://acct.blob.core.windows.net/ctr/file.csv",
                new_name="dry.csv",
                dry_run=True,
            )

        mock_catalog.entity.create_or_update.assert_not_called()

    @pytest.mark.unit
    def test_raises_when_entity_not_found(self):
        from azure.core.exceptions import ResourceNotFoundError
        from data_lake.utilities.utilities import update_purview_entity

        mock_catalog = MagicMock()
        mock_catalog.entity.get_by_unique_attributes.side_effect = ResourceNotFoundError("nope")

        with (
            patch(_PURVIEW_CRED_PATCH),
            patch(_CATALOG_PATCH, return_value=mock_catalog),
        ):
            with pytest.raises(ValueError, match="Entity not found"):
                update_purview_entity(
                    "purview",
                    "https://acct.blob.core.windows.net/ctr/missing.csv",
                    new_name="x.csv",
                )

    @pytest.mark.unit
    def test_directory_tries_container_type_first(self):
        """Paths ending with '/' should be tried as azure_blob_container first."""
        from data_lake.utilities.utilities import update_purview_entity

        mock_catalog = MagicMock()
        container_entity = _make_entity_result(
            guid="ctr-guid",
            type_name="azure_blob_container",
            name="ctr",
            qualified_name="https://acct.blob.core.windows.net/ctr",
            user_description="container desc",
        )
        mock_catalog.entity.get_by_unique_attributes.return_value = container_entity
        mock_catalog.entity.create_or_update.return_value = MagicMock()

        with (
            patch(_PURVIEW_CRED_PATCH),
            patch(_CATALOG_PATCH, return_value=mock_catalog),
        ):
            update_purview_entity(
                "purview",
                "https://acct.blob.core.windows.net/ctr/",
                new_description="updated container desc",
            )

        first_call_kwargs = mock_catalog.entity.get_by_unique_attributes.call_args_list[0].kwargs
        assert first_call_kwargs.get("type_name") == "azure_blob_container"

        call = mock_catalog.entity.create_or_update.call_args
        body = call.kwargs.get("entity") or call[1].get("entity")
        assert body["entity"]["typeName"] == "azure_blob_container"
        assert body["entity"]["attributes"]["userDescription"] == "updated container desc"


# ═══════════════════════════════════════════════════════════════════════════
# Utilities – list_artifact_registry
# ═══════════════════════════════════════════════════════════════════════════

_SEARCH_CLIENT_PATCH = "data_lake.utilities.utilities.SearchClient"


class TestListArtifactRegistry:
    """Tests for data_lake.utilities.utilities.list_artifact_registry."""

    @pytest.mark.unit
    def test_returns_all_results(self):
        from data_lake.utilities.utilities import list_artifact_registry

        doc1 = {"artifact_id": "id1", "name": "file1.csv"}
        doc2 = {"artifact_id": "id2", "name": "file2.csv"}
        mock_search_client = MagicMock()
        mock_search_client.search.return_value = iter([doc1, doc2])

        with (
            patch(_SEARCH_CRED_PATCH),
            patch(_SEARCH_CLIENT_PATCH, return_value=mock_search_client),
        ):
            results = list_artifact_registry("test-search")

        assert len(results) == 2
        assert results[0]["artifact_id"] == "id1"
        assert results[1]["artifact_id"] == "id2"

    @pytest.mark.unit
    def test_passes_filter_expression(self):
        from data_lake.utilities.utilities import list_artifact_registry

        mock_search_client = MagicMock()
        mock_search_client.search.return_value = iter([])

        with (
            patch(_SEARCH_CRED_PATCH),
            patch(_SEARCH_CLIENT_PATCH, return_value=mock_search_client),
        ):
            list_artifact_registry("test-search", filter_expression="domain eq 'energy'")

        call_kwargs = mock_search_client.search.call_args.kwargs
        assert call_kwargs.get("filter") == "domain eq 'energy'"

    @pytest.mark.unit
    def test_passes_select_fields(self):
        from data_lake.utilities.utilities import list_artifact_registry

        mock_search_client = MagicMock()
        mock_search_client.search.return_value = iter([])

        with (
            patch(_SEARCH_CRED_PATCH),
            patch(_SEARCH_CLIENT_PATCH, return_value=mock_search_client),
        ):
            list_artifact_registry("test-search", select_fields=["artifact_id", "name"])

        call_kwargs = mock_search_client.search.call_args.kwargs
        assert call_kwargs.get("select") == ["artifact_id", "name"]

    @pytest.mark.unit
    def test_default_index_name(self):
        from data_lake.utilities.utilities import list_artifact_registry

        mock_search_client = MagicMock()
        mock_search_client.search.return_value = iter([])

        captured = {}

        def fake_search_client_ctor(**kwargs):
            captured.update(kwargs)
            return mock_search_client

        with (
            patch(_SEARCH_CRED_PATCH),
            patch(_SEARCH_CLIENT_PATCH, side_effect=fake_search_client_ctor),
        ):
            list_artifact_registry("test-search")

        assert captured.get("index_name") == "artifact-registry"

    @pytest.mark.unit
    def test_custom_index_name(self):
        from data_lake.utilities.utilities import list_artifact_registry

        mock_search_client = MagicMock()
        mock_search_client.search.return_value = iter([])

        captured = {}

        def fake_search_client_ctor(**kwargs):
            captured.update(kwargs)
            return mock_search_client

        with (
            patch(_SEARCH_CRED_PATCH),
            patch(_SEARCH_CLIENT_PATCH, side_effect=fake_search_client_ctor),
        ):
            list_artifact_registry("test-search", index_name="my-registry")

        assert captured.get("index_name") == "my-registry"

    @pytest.mark.unit
    def test_returns_empty_list_when_no_results(self):
        from data_lake.utilities.utilities import list_artifact_registry

        mock_search_client = MagicMock()
        mock_search_client.search.return_value = iter([])

        with (
            patch(_SEARCH_CRED_PATCH),
            patch(_SEARCH_CLIENT_PATCH, return_value=mock_search_client),
        ):
            results = list_artifact_registry("test-search")

        assert results == []


# ═══════════════════════════════════════════════════════════════════════════
# Utility manifest runner
# ═══════════════════════════════════════════════════════════════════════════


class TestUtilityManifest:
    @pytest.mark.unit
    def test_requires_at_least_one_operation(self):
        with pytest.raises(ValueError, match="at least one operation"):
            UtilityManifest()  # type: ignore[call-arg]

    @pytest.mark.unit
    def test_requires_purview_account_for_updates(self):
        with pytest.raises(ValueError, match="purview_account"):
            UtilityManifest(  # type: ignore[call-arg]
                entity_updates=[
                    PurviewEntityUpdateConfig(  # type: ignore[call-arg]
                        qualified_name="https://acct.blob.core.windows.net/ctr/file.csv",
                        new_name="renamed.csv",
                    )
                ]
            )

    @pytest.mark.unit
    def test_round_trip_yaml(self, tmp_path):
        manifest = UtilityManifest(  # type: ignore[call-arg]
            purview_account="agora-purview",
            registry_query=ArtifactRegistryQueryConfig(  # type: ignore[call-arg]
                search_service="agora-ai-search",
                top=5,
            ),
            entity_updates=[
                PurviewEntityUpdateConfig(  # type: ignore[call-arg]
                    qualified_name="https://acct.blob.core.windows.net/ctr/file.csv",
                    new_description="updated",
                )
            ],
        )

        manifest_path = tmp_path / "utility_manifest.yaml"
        manifest.to_yaml(manifest_path)

        loaded = UtilityManifest.from_yaml(manifest_path)
        assert loaded.purview_account == "agora-purview"
        assert loaded.registry_query is not None
        assert loaded.registry_query.search_service == "agora-ai-search"
        assert loaded.entity_updates[0].new_description == "updated"


class TestRunUtilityManifest:
    @pytest.mark.unit
    def test_runs_query_and_updates_and_writes_output(self, tmp_path):
        from data_lake.manifest.run_manifest import run_manifest

        output_path = tmp_path / "registry.json"
        manifest = UtilityManifest(  # type: ignore[call-arg]
            purview_account="agora-purview",
            registry_query=ArtifactRegistryQueryConfig(  # type: ignore[call-arg]
                search_service="agora-ai-search",
                filter_expression="domain eq 'powergrid'",
                top=2,
                output_path=str(output_path),
            ),
            entity_updates=[
                PurviewEntityUpdateConfig(  # type: ignore[call-arg]
                    qualified_name="https://acct.blob.core.windows.net/ctr/file.csv",
                    new_name="renamed.csv",
                )
            ],
        )

        with (
            patch(
                "data_lake.manifest.run_manifest.list_artifact_registry",
                return_value=[{"artifact_id": "id1"}],
            ) as mock_list,
            patch("data_lake.manifest.run_manifest.update_purview_entity") as mock_update,
        ):
            results = run_manifest(manifest, dry_run=True)

        mock_list.assert_called_once_with(
            search_service="agora-ai-search",
            index_name="artifact-registry",
            filter_expression="domain eq 'powergrid'",
            top=2,
            select_fields=None,
        )
        mock_update.assert_called_once_with(
            "agora-purview",
            "https://acct.blob.core.windows.net/ctr/file.csv",
            new_name="renamed.csv",
            new_description=None,
            dry_run=True,
        )
        assert results["would_update_entities"] == 1
        assert results["artifact_registry_results"] == [{"artifact_id": "id1"}]
        assert yaml.safe_load(output_path.read_text()) == [{"artifact_id": "id1"}]

    @pytest.mark.unit
    def test_dry_run_cli_flag(self):
        from data_lake.manifest.run_manifest import run_manifest

        manifest = UtilityManifest(  # type: ignore[call-arg]
            purview_account="agora-purview",
            entity_updates=[
                PurviewEntityUpdateConfig(  # type: ignore[call-arg]
                    qualified_name="https://acct.blob.core.windows.net/ctr/file.csv",
                    new_description="updated",
                )
            ],
        )

        with patch("data_lake.manifest.run_manifest.update_purview_entity") as mock_update:
            run_manifest(manifest, dry_run=True)

        assert mock_update.call_args.kwargs["dry_run"] is True
