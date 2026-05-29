"""Tests for catalog config parsing."""

import pytest

from ....data_access.catalog.config import (
    CatalogConfig,
    FileOverride,
    SearchConfig,
    SourceConfig,
)


class TestSourceConfig:
    """Tests for SourceConfig model."""

    def test_local_source_type(self):
        source = SourceConfig(path="/data/weather/")
        assert source.source_type == "local"

    def test_blob_source_type(self):
        source = SourceConfig(path="az://account/container/prefix/")
        assert source.source_type == "blob"

    def test_blob_https_source_type(self):
        source = SourceConfig(path="https://orfb0eastus.blob.core.windows.net/fingerprints/.amltconfig")
        assert source.source_type == "blob"

    def test_relative_path_is_local(self):
        source = SourceConfig(path="./data/weather/")
        assert source.source_type == "local"

    def test_source_with_files_overrides(self):
        source = SourceConfig(
            path="/data/weather/",
            domain="earthscience",
            files={"daily_obs.csv": FileOverride(description="Daily observations")},
        )
        assert source.files["daily_obs.csv"].description == "Daily observations"


class TestSearchConfig:
    """Tests for SearchConfig model."""

    def test_defaults(self):
        cfg = SearchConfig()
        assert cfg.embedding_model == "azure-openai"
        assert cfg.hybrid_alpha == 0.5
        assert cfg.azure_openai_endpoint is None

    def test_custom_model(self):
        cfg = SearchConfig(embedding_model="all-MiniLM-L6-v2")
        assert cfg.embedding_model == "all-MiniLM-L6-v2"

    def test_alpha_bounds(self):
        with pytest.raises(Exception):
            SearchConfig(hybrid_alpha=1.5)
        with pytest.raises(Exception):
            SearchConfig(hybrid_alpha=-0.1)


class TestCatalogConfig:
    """Tests for CatalogConfig loading from YAML."""

    def test_from_yaml_minimal(self, tmp_path):
        config_file = tmp_path / "catalog.yaml"
        config_file.write_text("sources:\n  - path: /data/weather/\n    domain: earthscience\n")
        cfg = CatalogConfig.from_yaml(config_file)
        assert len(cfg.sources) == 1
        assert cfg.sources[0].path == "/data/weather/"
        assert cfg.sources[0].domain == "earthscience"
        assert cfg.search.embedding_model == "azure-openai"

    def test_from_yaml_full(self, tmp_path):
        config_file = tmp_path / "catalog.yaml"
        config_file.write_text(
            "sources:\n"
            "  - path: /data/weather/\n"
            "    domain: earthscience\n"
            "    description: Weather data\n"
            "    files:\n"
            "      daily_obs.csv:\n"
            "        description: Daily observations\n"
            "  - path: az://account/container/grid/\n"
            "    domain: powergrid\n"
            "search:\n"
            "  embedding_model: all-MiniLM-L6-v2\n"
            "  hybrid_alpha: 0.7\n"
        )
        cfg = CatalogConfig.from_yaml(config_file)
        assert len(cfg.sources) == 2
        assert cfg.sources[0].files["daily_obs.csv"].description == "Daily observations"
        assert cfg.sources[1].source_type == "blob"
        assert cfg.search.embedding_model == "all-MiniLM-L6-v2"
        assert cfg.search.hybrid_alpha == 0.7

    def test_from_yaml_not_found(self):
        with pytest.raises(FileNotFoundError):
            CatalogConfig.from_yaml("/nonexistent/catalog.yaml")

    def test_empty_yaml(self, tmp_path):
        config_file = tmp_path / "catalog.yaml"
        config_file.write_text("")
        cfg = CatalogConfig.from_yaml(config_file)
        assert cfg.sources == []
