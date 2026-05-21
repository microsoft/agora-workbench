"""Tests for catalog SQLite database."""

import pytest

from ....code_execution.data_access.catalog.db import (
    CatalogDB,
    artifact_id_from_uri,
)


@pytest.fixture
def db():
    """Create an in-memory catalog database for testing."""
    catalog_db = CatalogDB(db_path=":memory:", vec_dimensions=4)
    catalog_db.open()
    yield catalog_db
    catalog_db.close()


class TestArtifactIdGeneration:
    """Tests for artifact_id_from_uri."""

    def test_deterministic(self):
        uri = "/data/weather/daily_obs.csv"
        assert artifact_id_from_uri(uri) == artifact_id_from_uri(uri)

    def test_different_uris_different_ids(self):
        assert artifact_id_from_uri("/a.csv") != artifact_id_from_uri("/b.csv")

    def test_returns_16_char_hex(self):
        result = artifact_id_from_uri("/data/test.csv")
        assert len(result) == 16
        assert all(c in "0123456789abcdef" for c in result)


class TestCatalogDBBasicOps:
    """Tests for basic CRUD operations."""

    def test_upsert_and_get(self, db):
        db.upsert_artifact(
            artifact_id="test1",
            name="test.csv",
            storage_uri="/data/test.csv",
            description="A test file",
            domain="testing",
            source_type="local",
            indexed_at="2026-01-01T00:00:00Z",
        )
        record = db.get_artifact("test1")
        assert record is not None
        assert record.name == "test.csv"
        assert record.domain == "testing"
        assert record.description == "A test file"

    def test_get_nonexistent(self, db):
        assert db.get_artifact("nonexistent") is None

    def test_upsert_replaces(self, db):
        db.upsert_artifact(
            artifact_id="test1",
            name="old.csv",
            storage_uri="/data/test.csv",
            indexed_at="2026-01-01T00:00:00Z",
        )
        db.upsert_artifact(
            artifact_id="test1",
            name="new.csv",
            storage_uri="/data/test.csv",
            indexed_at="2026-01-02T00:00:00Z",
        )
        record = db.get_artifact("test1")
        assert record.name == "new.csv"

    def test_get_existing_uris(self, db):
        db.upsert_artifact(artifact_id="a", name="a.csv", storage_uri="/a.csv", indexed_at="2026-01-01T00:00:00Z")
        db.upsert_artifact(artifact_id="b", name="b.csv", storage_uri="/b.csv", indexed_at="2026-01-01T00:00:00Z")
        uris = db.get_existing_uris()
        assert uris == {"/a.csv", "/b.csv"}

    def test_delete_artifacts(self, db):
        db.upsert_artifact(artifact_id="a", name="a.csv", storage_uri="/a.csv", indexed_at="2026-01-01T00:00:00Z")
        db.upsert_artifact(artifact_id="b", name="b.csv", storage_uri="/b.csv", indexed_at="2026-01-01T00:00:00Z")
        db.delete_artifacts(["a"])
        assert db.get_artifact("a") is None
        assert db.get_artifact("b") is not None

    def test_list_domains(self, db):
        db.upsert_artifact(
            artifact_id="a",
            name="a.csv",
            storage_uri="/a.csv",
            domain="weather",
            indexed_at="2026-01-01T00:00:00Z",
        )
        db.upsert_artifact(
            artifact_id="b",
            name="b.csv",
            storage_uri="/b.csv",
            domain="energy",
            indexed_at="2026-01-01T00:00:00Z",
        )
        domains = db.list_domains()
        assert domains == ["energy", "weather"]


class TestCatalogDBSearch:
    """Tests for hybrid search."""

    def test_fts_search(self, db):
        db.upsert_artifact(
            artifact_id="weather1",
            name="daily_obs.csv",
            storage_uri="/data/weather/daily_obs.csv",
            description="NOAA daily weather observations",
            domain="earthscience",
            indexed_at="2026-01-01T00:00:00Z",
        )
        db.upsert_artifact(
            artifact_id="grid1",
            name="transmission_lines.geojson",
            storage_uri="/data/grid/lines.geojson",
            description="US power grid transmission lines",
            domain="powergrid",
            indexed_at="2026-01-01T00:00:00Z",
        )
        results = db.search(query="weather observations")
        assert len(results) >= 1
        assert results[0].id == "weather1"

    def test_fts_search_with_domain_filter(self, db):
        db.upsert_artifact(
            artifact_id="weather1",
            name="daily_obs.csv",
            storage_uri="/data/weather/daily_obs.csv",
            description="NOAA daily weather observations",
            domain="earthscience",
            indexed_at="2026-01-01T00:00:00Z",
        )
        db.upsert_artifact(
            artifact_id="weather2",
            name="weather_stations.csv",
            storage_uri="/data/grid/weather.csv",
            description="Weather station metadata for grid ops",
            domain="powergrid",
            indexed_at="2026-01-01T00:00:00Z",
        )
        results = db.search(query="weather", domain="powergrid")
        assert all(r.domain == "powergrid" for r in results)

    def test_vector_search(self, db):
        # Insert artifacts with embeddings (4-dim for test)
        db.upsert_artifact(
            artifact_id="a",
            name="weather.csv",
            storage_uri="/weather.csv",
            indexed_at="2026-01-01T00:00:00Z",
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        db.upsert_artifact(
            artifact_id="b",
            name="grid.csv",
            storage_uri="/grid.csv",
            indexed_at="2026-01-01T00:00:00Z",
            embedding=[0.0, 1.0, 0.0, 0.0],
        )
        # Query closer to "a"
        results = db.search(query="", query_embedding=[0.9, 0.1, 0.0, 0.0])
        assert len(results) >= 1
        assert results[0].id == "a"

    def test_empty_query_returns_all(self, db):
        db.upsert_artifact(
            artifact_id="a",
            name="a.csv",
            storage_uri="/a.csv",
            domain="test",
            indexed_at="2026-01-01T00:00:00Z",
        )
        results = db.search(query="")
        assert len(results) == 1

    def test_to_dict(self, db):
        db.upsert_artifact(
            artifact_id="a",
            name="test.csv",
            storage_uri="/test.csv",
            domain="testing",
            indexed_at="2026-01-01T00:00:00Z",
        )
        record = db.get_artifact("a")
        d = record.to_dict()
        assert d["id"] == "a"
        assert d["name"] == "test.csv"
        assert "score" not in d  # No score unless from search


class TestCatalogDBReadonlyQuery:
    """Tests for execute_readonly."""

    @pytest.fixture
    def file_db(self, tmp_path):
        """Create an on-disk catalog database (required for read-only connections)."""
        db_path = tmp_path / "catalog.db"
        catalog_db = CatalogDB(db_path=str(db_path), vec_dimensions=4)
        catalog_db.open()
        catalog_db.upsert_artifact(
            artifact_id="a",
            name="weather.csv",
            storage_uri="/data/weather.csv",
            description="Weather data",
            domain="earthscience",
            source_type="local",
            content_type="text/csv",
            size_bytes=1024,
            indexed_at="2026-01-01T00:00:00Z",
        )
        catalog_db.upsert_artifact(
            artifact_id="b",
            name="grid.parquet",
            storage_uri="/data/grid.parquet",
            description="Grid topology",
            domain="powergrid",
            source_type="local",
            content_type="application/x-parquet",
            size_bytes=5000000,
            indexed_at="2026-01-02T00:00:00Z",
        )
        yield catalog_db
        catalog_db.close()

    def test_select_all(self, file_db):
        results = file_db.execute_readonly("SELECT id, name FROM artifacts ORDER BY name")
        assert len(results) == 2
        assert results[0]["name"] == "grid.parquet"
        assert results[1]["name"] == "weather.csv"

    def test_filter_by_content_type(self, file_db):
        results = file_db.execute_readonly("SELECT name FROM artifacts WHERE content_type = 'text/csv'")
        assert len(results) == 1
        assert results[0]["name"] == "weather.csv"

    def test_filter_by_size(self, file_db):
        results = file_db.execute_readonly("SELECT name FROM artifacts WHERE size_bytes > 100000")
        assert len(results) == 1
        assert results[0]["name"] == "grid.parquet"

    def test_aggregation(self, file_db):
        results = file_db.execute_readonly(
            "SELECT domain, COUNT(*) as cnt FROM artifacts GROUP BY domain ORDER BY domain"
        )
        assert len(results) == 2
        assert results[0]["domain"] == "earthscience"
        assert results[0]["cnt"] == 1

    def test_max_rows_limit(self, file_db):
        results = file_db.execute_readonly("SELECT * FROM artifacts", max_rows=1)
        assert len(results) == 1

    def test_rejects_insert(self, file_db):
        with pytest.raises(ValueError, match="Write operations"):
            file_db.execute_readonly(
                "INSERT INTO artifacts (id, name, storage_uri, indexed_at) VALUES ('x', 'x', 'x', 'x')"
            )

    def test_rejects_delete(self, file_db):
        with pytest.raises(ValueError, match="Write operations"):
            file_db.execute_readonly("DELETE FROM artifacts WHERE id = 'a'")

    def test_rejects_drop(self, file_db):
        with pytest.raises(ValueError, match="Write operations"):
            file_db.execute_readonly("DROP TABLE artifacts")

    def test_fts_match_query(self, file_db):
        results = file_db.execute_readonly(
            "SELECT a.name FROM artifacts_fts fts JOIN artifacts a ON a.rowid = fts.rowid "
            "WHERE artifacts_fts MATCH 'weather'"
        )
        assert len(results) == 1
        assert results[0]["name"] == "weather.csv"
