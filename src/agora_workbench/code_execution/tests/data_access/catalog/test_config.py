"""Tests for catalog config parsing."""

import math

import pytest

from ....data_access.catalog.config import (
    CatalogConfig,
    FileOverride,
    SearchConfig,
    SourceConfig,
)


class TestFileOverride:
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("artifact_id", ""),
            ("artifact_id", " "),
            ("artifact_id", ":bad"),
            ("artifact_id", "bad:"),
            ("aliases", [""]),
            ("aliases", [" external:id"]),
            ("aliases", [":bad"]),
            ("aliases", ["bad:"]),
        ],
    )
    def test_rejects_empty_or_malformed_identity_values(self, field, value):
        with pytest.raises(ValueError, match="non-empty|Invalid"):
            FileOverride(**{field: value})

    def test_accepts_opaque_and_namespaced_identity_values(self):
        override = FileOverride(
            artifact_id="external:weather",
            aliases=["legacy-id", "source-key:weather"],
        )

        assert override.artifact_id == "external:weather"
        assert override.aliases == ["legacy-id", "source-key:weather"]


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
        assert source.path == "az://orfb0eastus/fingerprints/.amltconfig"

    @pytest.mark.parametrize(
        "path",
        [
            "https://account123.dfs.core.windows.net/container/path",
            "abfss://container@account123.dfs.core.windows.net/path",
        ],
    )
    def test_adls_source_forms_are_supported(self, path):
        source = SourceConfig(path=path)
        assert source.source_type == "blob"
        assert source.path == "az://account123/container/path"

    @pytest.mark.parametrize("container", ["$root", "$web", "$logs"])
    def test_azure_system_containers_are_supported(self, container):
        source = SourceConfig(path=f"az://account123/{container}")
        assert source.path == f"az://account123/{container}"

    def test_blob_prefix_normalization_preserves_directory_boundary(self):
        without_slash = SourceConfig(path="az://account123/container/data")
        with_slash = SourceConfig(path="az://account123/container/data/")
        assert without_slash.path == "az://account123/container/data"
        assert with_slash.path == "az://account123/container/data/"

    @pytest.mark.parametrize(
        "path",
        [
            "https://blob.core.windows.net/container/path",
            "https://dfs.core.windows.net/container/path",
            "az://ab/container/path",
            "az://account123/ab/path",
            "az://account123/Bad_Container/path",
            "https://account123.blob.core.windows.net:8443/container/path",
        ],
    )
    def test_malformed_azure_sources_are_rejected(self, path):
        with pytest.raises(Exception):
            SourceConfig(path=path)

    def test_validation_error_does_not_include_sas_or_userinfo(self):
        secret = "DO_NOT_LOG"
        with pytest.raises(Exception, match="must not contain user information") as exc_info:
            SourceConfig(path=f"https://user:{secret}@account123.blob.core.windows.net/container/path?sig={secret}")
        assert secret not in str(exc_info.value)

    def test_abfss_validation_error_does_not_include_password(self):
        secret = "DO_NOT_LOG"
        with pytest.raises(Exception) as exc_info:
            SourceConfig(path=f"abfss://container:{secret}@account123.dfs.core.windows.net/path?sig={secret}")
        assert secret not in str(exc_info.value)

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

    def test_local_manifest_rejects_remote_uri(self):
        with pytest.raises(ValueError, match="local manifest path"):
            SourceConfig(
                source_id="local",
                path="/data/weather",
                discovery="manifest",
                manifest="az://account123/container/manifest.json",
            )

    @pytest.mark.parametrize("stale_limit", [math.inf, -math.inf, math.nan])
    def test_manifest_stale_limit_must_be_finite(self, stale_limit):
        with pytest.raises(ValueError, match="finite|greater than or equal"):
            SourceConfig(
                source_id="local",
                path="/data/weather",
                discovery="manifest",
                manifest="manifest.json",
                max_stale_seconds=stale_limit,
            )


class TestSearchConfig:
    """Tests for SearchConfig model."""

    def test_defaults(self):
        cfg = SearchConfig()
        assert cfg.embedding_model == "none"
        assert cfg.embedding_dimensions is None
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

    def test_embedding_dimensions_are_optional_and_positive(self):
        assert SearchConfig(embedding_dimensions=None).embedding_dimensions is None
        assert SearchConfig(embedding_dimensions=1536).embedding_dimensions == 1536
        with pytest.raises(Exception):
            SearchConfig(embedding_dimensions=0)


class TestCatalogConfig:
    """Tests for CatalogConfig loading from YAML."""

    def test_from_yaml_minimal(self, tmp_path):
        config_file = tmp_path / "catalog.yaml"
        config_file.write_text("sources:\n  - path: /data/weather/\n    domain: earthscience\n")
        cfg = CatalogConfig.from_yaml(config_file)
        assert len(cfg.sources) == 1
        assert cfg.sources[0].path == "/data/weather/"
        assert cfg.sources[0].domain == "earthscience"
        assert cfg.search.embedding_model == "none"

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
            "  embedding_dimensions: 384\n"
            "  hybrid_alpha: 0.7\n"
        )
        cfg = CatalogConfig.from_yaml(config_file)
        assert len(cfg.sources) == 2
        assert cfg.sources[0].files["daily_obs.csv"].description == "Daily observations"
        assert cfg.sources[1].source_type == "blob"
        assert cfg.search.embedding_model == "all-MiniLM-L6-v2"
        assert cfg.search.embedding_dimensions == 384
        assert cfg.search.hybrid_alpha == 0.7

    def test_from_yaml_not_found(self):
        with pytest.raises(FileNotFoundError):
            CatalogConfig.from_yaml("/nonexistent/catalog.yaml")

    def test_empty_yaml(self, tmp_path):
        config_file = tmp_path / "catalog.yaml"
        config_file.write_text("")
        cfg = CatalogConfig.from_yaml(config_file)
        assert cfg.sources == []
