"""Tests for catalog SQLite database."""

import sqlite3
import struct
import threading
import time

import pytest

from ....data_access.catalog import db as db_module
from ....data_access.catalog.db import (
    SCHEMA_VERSION,
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

    def test_upsert_without_source_root_preserves_existing_root(self, db):
        db.upsert_artifact(
            artifact_id="a",
            source_id="weather",
            source_type="local",
            source_root="/data/weather",
            name="a.csv",
            storage_uri="/data/weather/a.csv",
        )
        db.upsert_artifact(
            artifact_id="b",
            source_id="weather",
            source_type="local",
            name="b.csv",
            storage_uri="/data/weather/b.csv",
        )

        source = db.conn.execute("SELECT root_uri FROM catalog_sources WHERE source_id = 'weather'").fetchone()
        assert source["root_uri"] == "/data/weather"

    def test_delete_artifacts(self, db):
        db.upsert_artifact(artifact_id="a", name="a.csv", storage_uri="/a.csv", indexed_at="2026-01-01T00:00:00Z")
        db.upsert_artifact(artifact_id="b", name="b.csv", storage_uri="/b.csv", indexed_at="2026-01-01T00:00:00Z")
        db.delete_artifacts(["a"])
        assert db.get_artifact("a") is None
        assert db.get_artifact("b") is not None

    def test_revisions_and_tombstones_are_retained(self, db):
        db.upsert_artifact(
            artifact_id="stable",
            source_id="weather",
            logical_path="daily.csv",
            name="daily.csv",
            storage_uri="/old/daily.csv",
            content_revision="content-1",
            metadata_revision="metadata-1",
            indexed_at="2026-01-01T00:00:00Z",
        )
        db.upsert_artifact(
            artifact_id="stable",
            source_id="weather",
            logical_path="daily.csv",
            name="daily.csv",
            storage_uri="/new/daily.csv",
            content_revision="content-1",
            metadata_revision="metadata-1",
            indexed_at="2026-01-02T00:00:00Z",
        )
        db.delete_artifacts(["stable"], deleted_at="2026-01-03T00:00:00Z")

        assert db.get_artifact("stable") is None
        tombstone = db.get_artifact("stable", include_deleted=True)
        assert tombstone is not None
        assert tombstone.current_revision == 3
        assert tombstone.deleted_at == "2026-01-03T00:00:00Z"
        assert db.get_artifact("stable", revision=1).storage_uri == "/old/daily.csv"
        assert db.get_artifact("stable", revision=2).storage_uri == "/new/daily.csv"
        assert len(db.list_revisions("stable")) == 3

    def test_purge_deleted_compares_timestamp_instants(self, db):
        for artifact_id, deleted_at in (
            ("older", "2026-01-02T23:59:59Z"),
            ("boundary-z", "2026-01-03T00:00:00Z"),
            ("boundary-offset", "2026-01-03T00:00:00+00:00"),
        ):
            db.upsert_artifact(
                artifact_id=artifact_id,
                name=f"{artifact_id}.csv",
                storage_uri=f"/{artifact_id}.csv",
                indexed_at="2026-01-01T00:00:00Z",
            )
            db.delete_artifacts([artifact_id], deleted_at=deleted_at)

        assert db.purge_deleted("2026-01-03T00:00:00+00:00") == 1
        assert db.get_artifact("older", include_deleted=True) is None
        assert db.get_artifact("boundary-z", include_deleted=True) is not None
        assert db.get_artifact("boundary-offset", include_deleted=True) is not None

    def test_alias_lookup_and_collision_rejection(self, db):
        first = db.upsert_artifact(
            artifact_id="canonical-1",
            source_id="weather",
            logical_path="one.csv",
            name="one.csv",
            storage_uri="/one.csv",
            aliases=["legacy-uri:old-one", "old-opaque-id"],
        )
        second = db.upsert_artifact(
            artifact_id="canonical-2",
            source_id="weather",
            logical_path="two.csv",
            name="two.csv",
            storage_uri="/two.csv",
        )

        assert db.resolve_artifact_id("legacy-uri:old-one") == first
        assert db.get_artifact("old-opaque-id").id == first
        with pytest.raises(ValueError, match="Alias collision"):
            db.add_alias("legacy-uri", "old-one", "weather", second)
        with pytest.raises(ValueError, match="collides with canonical"):
            db.add_alias("artifact-id", "canonical-2", "weather", first)
        with pytest.raises(ValueError, match="collides with an existing alias"):
            db.upsert_artifact(
                artifact_id="legacy-uri:old-one",
                source_id="weather",
                logical_path="three.csv",
                name="three.csv",
                storage_uri="/three.csv",
            )

    def test_checksum_is_not_used_as_identity(self, db):
        artifact_id = db.upsert_artifact(
            artifact_id=None,
            source_id="weather",
            logical_path="same.csv",
            name="same.csv",
            storage_uri="/same.csv",
            checksum_sha256="a" * 64,
        )
        record = db.get_artifact(artifact_id)
        assert record.id != record.checksum_sha256

    def test_default_content_revision_does_not_include_storage_uri(self, db):
        db.upsert_artifact(
            artifact_id="stable",
            source_id="source",
            logical_path="same.csv",
            name="same.csv",
            storage_uri="/first/same.csv",
            size_bytes=10,
        )
        first = db.get_artifact("stable")
        db.upsert_artifact(
            artifact_id="stable",
            source_id="source",
            logical_path="same.csv",
            name="same.csv",
            storage_uri="/second/same.csv",
            size_bytes=10,
        )
        second = db.get_artifact("stable")
        assert second.content_revision == first.content_revision
        assert second.current_revision == 2

    def test_same_storage_uri_can_have_distinct_logical_identities(self, db):
        db.upsert_artifact(
            artifact_id="one",
            source_id="source-one",
            logical_path="same.csv",
            name="same.csv",
            storage_uri="/shared/same.csv",
        )
        db.upsert_artifact(
            artifact_id="two",
            source_id="source-two",
            logical_path="same.csv",
            name="same.csv",
            storage_uri="/shared/same.csv",
        )
        assert db.get_artifact("one") is not None
        assert db.get_artifact("two") is not None

    def test_source_scoped_aliases_require_source_when_ambiguous(self, db):
        for source_id, artifact_id, path in (
            ("one", "canonical-one", "/one.csv"),
            ("two", "canonical-two", "/two.csv"),
        ):
            db.upsert_artifact(
                artifact_id=artifact_id,
                source_id=source_id,
                logical_path="same.csv",
                name="same.csv",
                storage_uri=path,
                aliases=["shared-id"],
            )

        assert db.resolve_artifact_id("shared-id", "one") == "canonical-one"
        assert db.resolve_artifact_id("shared-id", "two") == "canonical-two"
        with pytest.raises(ValueError, match="Ambiguous artifact alias"):
            db.resolve_artifact_id("shared-id")

    def test_configured_id_added_later_becomes_alias(self, db):
        canonical = db.upsert_artifact(
            artifact_id=None,
            source_id="source",
            logical_path="same.csv",
            name="same.csv",
            storage_uri="/same.csv",
        )
        returned = db.upsert_artifact(
            artifact_id="configured-id",
            source_id="source",
            logical_path="same.csv",
            name="same.csv",
            storage_uri="/same.csv",
        )
        assert returned == canonical
        assert db.resolve_artifact_id("configured-id", "source") == canonical

    def test_namespaced_configured_id_added_later_becomes_alias(self, db):
        canonical = db.upsert_artifact(
            artifact_id=None,
            source_id="source",
            logical_path="same.csv",
            name="same.csv",
            storage_uri="/same.csv",
        )
        returned = db.upsert_artifact(
            artifact_id="external:configured-id",
            source_id="source",
            logical_path="same.csv",
            name="same.csv",
            storage_uri="/same.csv",
        )
        assert returned == canonical
        assert db.resolve_artifact_id("external:configured-id", "source") == canonical

    def test_batch_upsert_is_atomic(self, db):
        with pytest.raises(ValueError, match="already assigned"):
            db.upsert_artifacts_batch(
                [
                    {
                        "artifact_id": "first",
                        "source_id": "source",
                        "logical_path": "first.csv",
                        "name": "first.csv",
                        "storage_uri": "/first.csv",
                    },
                    {
                        "artifact_id": "first",
                        "source_id": "other",
                        "logical_path": "other.csv",
                        "name": "other.csv",
                        "storage_uri": "/other.csv",
                    },
                ]
            )
        assert db.get_artifact("first") is None

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

    def test_open_reports_missing_fts5(self, monkeypatch):
        real_connect = sqlite3.connect

        class ConnectionWithoutFts5:
            def __init__(self):
                self._conn = real_connect(":memory:")

            def __getattr__(self, name):
                return getattr(self._conn, name)

            def __setattr__(self, name, value):
                if name == "_conn":
                    object.__setattr__(self, name, value)
                else:
                    setattr(self._conn, name, value)

            def execute(self, sql, parameters=()):
                if "__agora_fts5_probe" in sql:
                    raise sqlite3.OperationalError("no such module: fts5")
                return self._conn.execute(sql, parameters)

        monkeypatch.setattr(sqlite3, "connect", lambda *_args, **_kwargs: ConnectionWithoutFts5())

        catalog_db = CatalogDB(":memory:")
        with pytest.raises(RuntimeError, match="requires SQLite with FTS5 support"):
            catalog_db.open()


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

    def test_rejects_wrong_artifact_embedding_dimensions(self, db):
        with pytest.raises(ValueError, match="expected 4, got 2"):
            db.upsert_artifact(
                artifact_id="a",
                name="a.csv",
                storage_uri="/a.csv",
                indexed_at="2026-01-01T00:00:00Z",
                embedding=[1.0, 0.0],
            )

    def test_rejects_wrong_query_embedding_dimensions(self, db):
        with pytest.raises(ValueError, match="expected 4, got 2"):
            db.search(query="", query_embedding=[1.0, 0.0])

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

    def test_empty_string_filters_are_treated_as_absent(self, db):
        db.upsert_artifact(artifact_id="a", name="a.csv", storage_uri="/a.csv")
        assert [record.id for record in db.search("", domain="", source_type="")] == ["a"]

    def test_nonempty_no_match_does_not_browse(self, db):
        db.upsert_artifact(artifact_id="a", name="weather.csv", storage_uri="/a.csv")
        assert db.search(query="quantum") == []

    @pytest.mark.parametrize(
        ("query", "expected"),
        [('"', []), (":", []), ("weather - (daily)", ["a"]), ("weather OR", [])],
    )
    def test_punctuation_is_treated_as_literal_text(self, db, query, expected):
        db.upsert_artifact(
            artifact_id="a",
            name="weather-daily.csv",
            storage_uri="/a.csv",
            description="weather daily observations",
        )
        results = db.search(query=query)
        assert [result.id for result in results] == expected

    def test_filters_are_applied_before_vector_top_k(self, db):
        db.upsert_artifact(
            artifact_id="outside",
            source_id="one",
            logical_path="outside.csv",
            name="outside.csv",
            storage_uri="/outside.csv",
            domain="other",
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        db.upsert_artifact(
            artifact_id="inside",
            source_id="two",
            logical_path="inside.csv",
            name="inside.csv",
            storage_uri="/inside.csv",
            domain="wanted",
            embedding=[0.0, 1.0, 0.0, 0.0],
        )
        results = db.search("", query_embedding=[1.0, 0.0, 0.0, 0.0], domain="wanted", top=1)
        assert [result.id for result in results] == ["inside"]

    def test_vector_search_uses_bounded_candidates_and_loads_only_results(self, db):
        for index in range(10):
            db.upsert_artifact(
                artifact_id=f"artifact-{index}",
                name=f"artifact-{index}.csv",
                storage_uri=f"/artifact-{index}.csv",
                embedding=[float(index), 0.0, 0.0, 0.0],
            )
        statements = []
        db.conn.set_trace_callback(statements.append)
        try:
            results = db.search("", query_embedding=[0.0, 0.0, 0.0, 0.0], top=2)
        finally:
            db.conn.set_trace_callback(None)

        assert len(results) == 2
        traced_sql = "\n".join(statements).upper()
        assert "V.K = 6" in traced_sql
        assert "COUNT(*) FROM ARTIFACTS_VEC" not in traced_sql
        assert "SELECT * FROM ARTIFACTS WHERE ID IN" in traced_sql

    def test_deterministic_tie_breaking_and_bounded_top(self, db):
        for source_id, artifact_id in (("z-source", "z"), ("a-source", "a"), ("m-source", "m")):
            db.upsert_artifact(
                artifact_id=artifact_id,
                source_id=source_id,
                logical_path="same.csv",
                name="same.csv",
                storage_uri=f"/{artifact_id}.csv",
                description="identical searchable text",
            )
        assert [result.id for result in db.search("identical", top=2)] == ["a", "m"]
        assert db.search("identical", top=0) == []
        assert len(db.search("", top=1000)) == 3

    def test_delete_restore_keeps_fts_vectors_and_revisions_consistent(self, db):
        db.upsert_artifact(
            artifact_id="stable",
            source_id="source",
            logical_path="weather.csv",
            name="weather.csv",
            storage_uri="/weather.csv",
            description="old weather",
            content_revision="one",
            metadata_revision="one",
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        db.delete_artifacts(["stable"])
        assert db.search("weather") == []
        assert db.conn.execute("SELECT COUNT(*) FROM artifacts_vec WHERE id='stable'").fetchone()[0] == 0

        db.upsert_artifact(
            artifact_id="stable",
            source_id="source",
            logical_path="weather.csv",
            name="weather.csv",
            storage_uri="/weather.csv",
            description="restored weather",
            content_revision="two",
            metadata_revision="two",
            embedding=[0.0, 1.0, 0.0, 0.0],
            _replace_embedding=True,
        )
        assert [result.id for result in db.search("restored")] == ["stable"]
        assert db.conn.execute("SELECT COUNT(*) FROM artifacts_vec WHERE id='stable'").fetchone()[0] == 1
        assert len(db.list_revisions("stable")) == 3

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

    def test_delete_vectors_after_close_and_reopen_preserves_top_k(self, tmp_path):
        db_path = tmp_path / "catalog.db"
        catalog_db = CatalogDB(db_path, vec_dimensions=4)
        catalog_db.open()
        for artifact_id, embedding in (
            ("a", [1.0, 0.0, 0.0, 0.0]),
            ("b", [0.9, 0.1, 0.0, 0.0]),
            ("c", [0.8, 0.2, 0.0, 0.0]),
        ):
            catalog_db.upsert_artifact(
                artifact_id=artifact_id,
                name=f"{artifact_id}.csv",
                storage_uri=f"/{artifact_id}.csv",
                indexed_at="2026-01-01T00:00:00Z",
                embedding=embedding,
            )
        catalog_db.close()

        reopened = CatalogDB(db_path, vec_dimensions=None)
        reopened.open()
        reopened.delete_artifacts(["a"])
        assert reopened.vec_dimensions == 4
        results = reopened.search("", query_embedding=[1.0, 0.0, 0.0, 0.0], top=2, hybrid_alpha=0.0)
        assert [result.id for result in results] == ["b", "c"]
        assert reopened.conn.execute("SELECT COUNT(*) FROM artifacts_vec").fetchone()[0] == 2
        reopened.close()

        verified = CatalogDB(db_path, vec_dimensions=None)
        verified.open()
        assert verified.execute_readonly("SELECT COUNT(*) AS count FROM artifacts_vec")[0]["count"] == 2
        assert verified.vec_dimensions == 4
        verified.close()

    def test_orphan_reconciliation_is_committed(self, tmp_path):
        db_path = tmp_path / "catalog.db"
        catalog_db = CatalogDB(db_path, vec_dimensions=2)
        catalog_db.open()
        catalog_db.upsert_artifact(
            artifact_id="a",
            name="a.csv",
            storage_uri="/a.csv",
            indexed_at="2026-01-01T00:00:00Z",
            embedding=[1.0, 0.0],
        )
        catalog_db.conn.execute("DELETE FROM artifact_revisions WHERE artifact_id = 'a'")
        catalog_db.conn.execute("DELETE FROM artifacts WHERE id = 'a'")
        catalog_db.conn.commit()
        catalog_db.close()

        reconciled = CatalogDB(db_path, vec_dimensions=None)
        reconciled.open()
        assert reconciled.search("", query_embedding=[1.0, 0.0]) == []
        reconciled.close()

        verified = CatalogDB(db_path, vec_dimensions=None)
        verified.open()
        assert verified.execute_readonly("SELECT COUNT(*) AS count FROM artifacts_vec")[0]["count"] == 0
        verified.close()

    def test_vector_capability_does_not_commit_active_transaction(self, tmp_path):
        db_path = tmp_path / "catalog.db"
        catalog_db = CatalogDB(db_path, vec_dimensions=2)
        catalog_db.open()
        catalog_db.conn.execute(
            """INSERT INTO catalog_sources(source_id, source_type, root_uri, created_at)
               VALUES ('legacy', 'legacy', NULL, '2026-01-01T00:00:00Z')"""
        )
        catalog_db.conn.execute(
            """INSERT INTO artifacts
               (id, source_id, logical_path, name, storage_uri, indexed_at,
                content_revision, metadata_revision)
               VALUES ('pending', 'legacy', 'pending.csv', 'pending.csv',
                       '/pending.csv', '2026-01-01T00:00:00Z', 'unknown', 'pending')"""
        )

        catalog_db._ensure_vector_capability(catalog_db.conn)

        assert catalog_db.conn.in_transaction is True
        catalog_db.conn.rollback()
        assert catalog_db.get_artifact("pending") is None

        catalog_db.upsert_artifact(
            artifact_id="committed",
            name="committed.csv",
            storage_uri="/committed.csv",
            indexed_at="2026-01-01T00:00:00Z",
            embedding=[1.0, 0.0],
        )
        assert catalog_db.search("", query_embedding=[1.0, 0.0])[0].id == "committed"
        catalog_db.close()


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

    def test_in_memory_query_only_blocks_with_prefixed_writes(self, db):
        db.upsert_artifact(
            artifact_id="a",
            name="weather.csv",
            storage_uri="/data/weather.csv",
            indexed_at="2026-01-01T00:00:00Z",
        )

        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute_readonly("WITH doomed AS (SELECT id FROM artifacts) DELETE FROM artifacts")

        assert db.get_artifact("a") is not None
        db.upsert_artifact(
            artifact_id="b",
            name="grid.csv",
            storage_uri="/data/grid.csv",
            indexed_at="2026-01-02T00:00:00Z",
        )
        assert db.get_artifact("b") is not None

    def test_in_memory_vector_query_loads_capability_lazily(self, db, monkeypatch):
        db.upsert_artifact(
            artifact_id="a",
            name="weather.csv",
            storage_uri="/data/weather.csv",
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        db._vector_loaded = False
        imported = []
        original_import_module = db_module.import_module

        def tracked_import_module(name):
            imported.append(name)
            return original_import_module(name)

        monkeypatch.setattr(db_module, "import_module", tracked_import_module)

        assert db.execute_readonly("SELECT COUNT(*) AS count FROM artifacts_vec") == [{"count": 1}]
        assert imported == ["sqlite_vec"]

    def test_fts_match_query(self, file_db):
        results = file_db.execute_readonly(
            "SELECT a.name FROM artifacts_fts fts JOIN artifacts a ON a.rowid = fts.rowid "
            "WHERE artifacts_fts MATCH 'weather'"
        )
        assert len(results) == 1
        assert results[0]["name"] == "weather.csv"

    def test_vector_table_name_in_literals_and_comments_does_not_load_extension(self, file_db):
        results = file_db.execute_readonly("SELECT 'artifacts_vec' AS value /* artifacts_vec */ -- artifacts_vec\n")
        assert results == [{"value": "artifacts_vec"}]
        assert file_db._vector_loaded is False

    def test_wal_reader_sees_committed_snapshot_during_refresh(self, tmp_path, monkeypatch):
        path = tmp_path / "concurrent.db"
        catalog = CatalogDB(path, vec_dimensions=4)
        catalog.open()
        catalog.upsert_artifact(
            artifact_id="old",
            source_id="source",
            logical_path="old.csv",
            name="old.csv",
            storage_uri="/old.csv",
            source_type="local",
        )
        entered = threading.Event()
        release = threading.Event()
        original = catalog._record_source_refresh

        def pause_refresh(**kwargs):
            entered.set()
            assert release.wait(timeout=5)
            original(**kwargs)

        monkeypatch.setattr(catalog, "_record_source_refresh", pause_refresh)
        error = []

        def write_refresh():
            try:
                catalog.apply_refresh_batch(
                    [
                        {
                            "artifact_id": "new",
                            "source_id": "source",
                            "logical_path": "new.csv",
                            "name": "new.csv",
                            "storage_uri": "/new.csv",
                            "source_type": "local",
                        }
                    ],
                    ["old"],
                    [
                        {
                            "source_id": "source",
                            "source_type": "local",
                            "root_uri": "/",
                            "attempted_at": "2026-01-01T00:00:00Z",
                            "succeeded": True,
                            "artifact_count": 1,
                            "error": None,
                        }
                    ],
                )
            except Exception as exc:  # pragma: no cover - assertion reports the exception
                error.append(exc)

        thread = threading.Thread(target=write_refresh)
        thread.start()
        assert entered.wait(timeout=5)
        started = time.monotonic()
        rows = catalog.execute_readonly("SELECT id FROM artifacts WHERE deleted_at IS NULL ORDER BY id")
        elapsed = time.monotonic() - started
        assert rows == [{"id": "old"}]
        assert elapsed < 2
        assert catalog.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        release.set()
        thread.join(timeout=5)
        try:
            assert not error
            assert catalog.get_artifact("new") is not None
            assert catalog.get_artifact("old") is None
        finally:
            release.set()
            catalog.close()

    def test_public_readers_never_observe_rolled_back_refresh(self, tmp_path, monkeypatch):
        path = tmp_path / "rollback-readers.db"
        catalog = CatalogDB(path, vec_dimensions=4)
        catalog.open()
        catalog.apply_refresh_batch(
            [
                {
                    "artifact_id": "old",
                    "source_id": "source",
                    "logical_path": "old.csv",
                    "name": "old.csv",
                    "storage_uri": "/old.csv",
                    "description": "old committed data",
                    "domain": "committed",
                    "source_type": "local",
                    "aliases": ["legacy:old"],
                }
            ],
            [],
            [
                {
                    "source_id": "source",
                    "source_type": "local",
                    "root_uri": "/",
                    "attempted_at": "2026-01-01T00:00:00Z",
                    "succeeded": True,
                    "artifact_count": 1,
                    "error": None,
                }
            ],
        )
        entered = threading.Event()
        release = threading.Event()
        original = catalog._record_source_refresh

        def fail_after_status(**kwargs):
            original(**kwargs)
            entered.set()
            assert release.wait(timeout=5)
            raise RuntimeError("rollback refresh")

        monkeypatch.setattr(catalog, "_record_source_refresh", fail_after_status)
        errors = []

        def write_refresh():
            try:
                catalog.apply_refresh_batch(
                    [
                        {
                            "artifact_id": "new",
                            "source_id": "source",
                            "logical_path": "new.csv",
                            "name": "new.csv",
                            "storage_uri": "/new.csv",
                            "description": "new uncommitted data",
                            "domain": "uncommitted",
                            "source_type": "local",
                            "aliases": ["legacy:new"],
                        }
                    ],
                    ["old"],
                    [
                        {
                            "source_id": "source",
                            "source_type": "local",
                            "root_uri": "/",
                            "attempted_at": "2026-01-02T00:00:00Z",
                            "succeeded": True,
                            "artifact_count": 1,
                            "error": None,
                        }
                    ],
                )
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=write_refresh)
        thread.start()
        assert entered.wait(timeout=5)

        def assert_committed_view():
            assert catalog.get_artifact("old") is not None
            assert catalog.get_artifact("new") is None
            assert catalog.resolve_artifact_id("legacy:old") == "old"
            assert catalog.resolve_artifact_id("legacy:new") is None
            assert [record.current_revision for record in catalog.list_revisions("old")] == [1]
            assert catalog.list_revisions("new") == []
            assert catalog.find_by_source_path("source", "old.csv") is not None
            assert catalog.find_by_source_path("source", "new.csv") is None
            assert catalog.get_existing_uris("source") == {"/old.csv"}
            assert catalog.current_paths("source") == {"old.csv": "old"}
            assert catalog.list_domains() == ["committed"]
            assert [record.id for record in catalog.search("committed")] == ["old"]
            assert catalog.search("uncommitted") == []
            states = catalog.list_source_refresh_states()
            assert [(state.attempt_generation, state.artifact_count) for state in states] == [(1, 1)]
            assert catalog.get_source_refresh_state("source").attempt_generation == 1
            assert "new.csv" not in catalog.export_v0_json()

        assert_committed_view()
        release.set()
        thread.join(timeout=5)
        try:
            assert len(errors) == 1
            assert isinstance(errors[0], RuntimeError)
            assert_committed_view()
        finally:
            release.set()
            catalog.close()

    def test_in_memory_public_reader_waits_for_writer_rollback(self, db, monkeypatch):
        db.upsert_artifact(artifact_id="old", name="old.csv", storage_uri="/old.csv")
        entered = threading.Event()
        release = threading.Event()
        reader_finished = threading.Event()
        original = db._record_source_refresh

        def fail_refresh(**kwargs):
            original(**kwargs)
            entered.set()
            assert release.wait(timeout=5)
            raise RuntimeError("rollback refresh")

        monkeypatch.setattr(db, "_record_source_refresh", fail_refresh)

        def writer():
            with pytest.raises(RuntimeError, match="rollback"):
                db.apply_refresh_batch(
                    [
                        {
                            "artifact_id": "new",
                            "source_id": "legacy",
                            "logical_path": "new.csv",
                            "name": "new.csv",
                            "storage_uri": "/new.csv",
                            "source_type": "legacy",
                        }
                    ],
                    ["old"],
                    [
                        {
                            "source_id": "legacy",
                            "source_type": "legacy",
                            "root_uri": None,
                            "attempted_at": "2026-01-01T00:00:00Z",
                            "succeeded": True,
                            "artifact_count": 1,
                            "error": None,
                        }
                    ],
                )

        observed = []

        def reader():
            observed.append(db.get_artifact("old"))
            reader_finished.set()

        writer_thread = threading.Thread(target=writer)
        writer_thread.start()
        assert entered.wait(timeout=5)
        reader_thread = threading.Thread(target=reader)
        reader_thread.start()
        assert not reader_finished.wait(timeout=0.1)
        release.set()
        writer_thread.join(timeout=5)
        reader_thread.join(timeout=5)
        assert reader_finished.is_set()
        assert observed[0] is not None
        assert db.get_artifact("new") is None

    def test_search_uses_one_snapshot_during_concurrent_tombstone(self, tmp_path, monkeypatch):
        path = tmp_path / "search-race.db"
        catalog = CatalogDB(path, vec_dimensions=4)
        catalog.open()
        catalog.upsert_artifact(
            artifact_id="race",
            source_id="source",
            logical_path="race.csv",
            name="race.csv",
            storage_uri="/race.csv",
            description="race searchable",
            source_type="local",
        )
        entered = threading.Event()
        release = threading.Event()
        original = catalog._literal_fts_query

        def pause_after_eligible_snapshot(query):
            entered.set()
            assert release.wait(timeout=5)
            return original(query)

        monkeypatch.setattr(catalog, "_literal_fts_query", pause_after_eligible_snapshot)
        results = []
        errors = []

        def run_search():
            try:
                results.extend(catalog.search("race"))
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=run_search)
        thread.start()
        assert entered.wait(timeout=5)
        writer = CatalogDB(path, vec_dimensions=4)
        writer.open()
        try:
            writer.delete_artifacts(["race"])
        finally:
            writer.close()
        release.set()
        thread.join(timeout=5)
        try:
            assert errors == []
            assert [record.id for record in results] == ["race"]
            assert catalog.search("race") == []
        finally:
            release.set()
            catalog.close()


class TestCatalogDBRefreshTransactions:
    def test_refresh_batch_rolls_back_all_catalog_surfaces(self, db, monkeypatch):
        db.upsert_artifact(
            artifact_id="old",
            source_id="source",
            logical_path="old.csv",
            name="old.csv",
            storage_uri="/old.csv",
            description="old searchable",
            source_type="local",
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        original = db.upsert_artifact
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("injected batch failure")
            return original(*args, **kwargs)

        monkeypatch.setattr(db, "upsert_artifact", fail_second)
        rows = [
            {
                "artifact_id": artifact_id,
                "source_id": "source",
                "logical_path": f"{artifact_id}.csv",
                "name": f"{artifact_id}.csv",
                "storage_uri": f"/{artifact_id}.csv",
                "description": f"{artifact_id} searchable",
                "source_type": "local",
                "aliases": [f"legacy:{artifact_id}"],
                "embedding": [0.0, 1.0, 0.0, 0.0],
                "_replace_embedding": True,
            }
            for artifact_id in ("first", "second")
        ]
        with pytest.raises(RuntimeError, match="injected"):
            db.apply_refresh_batch(
                rows,
                ["old"],
                [
                    {
                        "source_id": "source",
                        "source_type": "local",
                        "root_uri": "/",
                        "attempted_at": "2026-01-01T00:00:00Z",
                        "succeeded": True,
                        "artifact_count": 2,
                        "error": None,
                    }
                ],
            )

        assert db.get_artifact("old") is not None
        assert db.search("old")[0].id == "old"
        assert db.conn.execute("SELECT COUNT(*) FROM artifacts_vec WHERE id='old'").fetchone()[0] == 1
        assert db.get_artifact("first") is None
        assert db.resolve_artifact_id("legacy:first") is None
        assert db.conn.execute("SELECT COUNT(*) FROM artifact_revisions WHERE artifact_id='first'").fetchone()[0] == 0
        assert (
            db.conn.execute("SELECT COUNT(*) FROM artifacts_fts WHERE artifacts_fts MATCH 'first'").fetchone()[0] == 0
        )
        assert db.conn.execute("SELECT COUNT(*) FROM artifacts_vec WHERE id='first'").fetchone()[0] == 0
        assert db.get_source_refresh_state("source") is None

    def test_vector_dimensions_and_model_state_are_validated(self, db):
        with pytest.raises(ValueError, match="provider dimension mismatch"):
            db.validate_vector_state("model-a", 3)
        db.validate_vector_state("model-a", 4)
        db.apply_refresh_batch([], [], [], "model-a")
        db.upsert_artifact(
            artifact_id="a",
            name="a.csv",
            storage_uri="/a.csv",
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        with pytest.raises(ValueError, match="model changed"):
            db.validate_vector_state("model-b", 4)
        with pytest.raises(ValueError, match="Embedding dimension mismatch"):
            db.upsert_artifact(
                artifact_id="b",
                name="b.csv",
                storage_uri="/b.csv",
                embedding=[1.0, 0.0],
            )


class TestCatalogSchemaMigration:
    def test_migrates_existing_records_and_preserves_legacy_id_alias(self, tmp_path):
        path = tmp_path / "legacy.db"
        storage_uri = "/data/weather.csv"
        legacy_id = artifact_id_from_uri(storage_uri)
        connection = sqlite3.connect(path)
        connection.execute(
            """CREATE TABLE artifacts (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, storage_uri TEXT NOT NULL UNIQUE,
                description TEXT, domain TEXT, source_type TEXT, content_type TEXT,
                size_bytes INTEGER, indexed_at TEXT NOT NULL
            )"""
        )
        connection.execute(
            "INSERT INTO artifacts VALUES (?, 'weather.csv', ?, 'Weather', 'earth', "
            "'local', 'text/csv', 12, '2026-01-01T00:00:00Z')",
            (legacy_id, storage_uri),
        )
        connection.commit()
        connection.close()

        catalog = CatalogDB(path, vec_dimensions=4)
        catalog.open()
        try:
            migrated = catalog.get_artifact(legacy_id)
            assert migrated is not None
            assert migrated.id != legacy_id
            assert migrated.logical_path.startswith(f"imported/{legacy_id}/")
            migrated_id = migrated.id
            catalog.upsert_artifact(
                artifact_id=migrated_id,
                source_id="weather",
                logical_path="weather.csv",
                name="weather.csv",
                storage_uri=storage_uri,
                source_type="local",
            )
            adopted = catalog.get_artifact(legacy_id)
            assert adopted.id == migrated_id
            assert adopted.source_id == "weather"
            assert adopted.logical_path == "weather.csv"
            assert catalog.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        finally:
            catalog.close()

        reopened = CatalogDB(path, vec_dimensions=4)
        reopened.open()
        try:
            assert reopened.get_artifact(legacy_id).id == migrated_id
        finally:
            reopened.close()

    @pytest.mark.parametrize("suffix", ["?token=DO_NOT_STORE", "#token=DO_NOT_STORE"])
    def test_migration_strips_az_query_and_fragment_credentials(self, tmp_path, suffix):
        path = tmp_path / "legacy-secret.db"
        storage_uri = f"az://account123/container/weather.csv{suffix}"
        legacy_id = artifact_id_from_uri(storage_uri)
        connection = sqlite3.connect(path)
        connection.execute(
            """CREATE TABLE artifacts (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, storage_uri TEXT NOT NULL UNIQUE,
                description TEXT, domain TEXT, source_type TEXT, content_type TEXT,
                size_bytes INTEGER, indexed_at TEXT NOT NULL
            )"""
        )
        connection.execute(
            "INSERT INTO artifacts VALUES (?, 'weather.csv', ?, NULL, NULL, "
            "'blob', 'text/csv', 12, '2026-01-01T00:00:00Z')",
            (legacy_id, storage_uri),
        )
        connection.commit()
        connection.close()

        catalog = CatalogDB(path, vec_dimensions=4)
        catalog.open()
        try:
            migrated = catalog.get_artifact(f"storage-uri:{storage_uri}")
            assert migrated is not None
            assert migrated.storage_uri == "az://account123/container/weather.csv"
            persisted = catalog.conn.execute(
                """SELECT storage_uri AS value FROM artifacts
                   UNION ALL
                   SELECT alias AS value FROM artifact_aliases"""
            ).fetchall()
            assert all("DO_NOT_STORE" not in row["value"] for row in persisted)
        finally:
            catalog.close()

    def test_future_schema_fails_without_mutation(self, tmp_path):
        path = tmp_path / "future.db"
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE marker(value TEXT)")
        connection.execute("INSERT INTO marker VALUES ('unchanged')")
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        connection.commit()
        connection.close()

        with pytest.raises(RuntimeError, match="newer than supported"):
            CatalogDB(path, vec_dimensions=4).open()

        connection = sqlite3.connect(path)
        try:
            assert connection.execute("SELECT value FROM marker").fetchone()[0] == "unchanged"
            assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION + 1
            assert (
                connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='artifacts'").fetchone()
                is None
            )
        finally:
            connection.close()

    def test_migration_preserves_and_rekeys_vector_embedding(self, tmp_path):
        sqlite_vec = pytest.importorskip("sqlite_vec")
        path = tmp_path / "legacy-vectors.db"
        connection = sqlite3.connect(path)
        connection.enable_load_extension(True)
        sqlite_vec.load(connection)
        connection.enable_load_extension(False)
        connection.execute(
            """CREATE TABLE artifacts (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, storage_uri TEXT NOT NULL UNIQUE,
                description TEXT, domain TEXT, source_type TEXT, content_type TEXT,
                size_bytes INTEGER, indexed_at TEXT NOT NULL
            )"""
        )
        connection.execute("CREATE VIRTUAL TABLE artifacts_vec USING vec0(id TEXT PRIMARY KEY, embedding float[4])")
        connection.execute(
            "INSERT INTO artifacts VALUES ('old-id', 'weather.csv', '/weather.csv', NULL, NULL, "
            "'local', 'text/csv', 12, '2026-01-01T00:00:00Z')"
        )
        embedding = struct.pack("4f", 1.0, 0.0, 0.0, 0.0)
        connection.execute("INSERT INTO artifacts_vec VALUES ('old-id', ?)", (embedding,))
        connection.commit()
        connection.close()

        catalog = CatalogDB(path, vec_dimensions=4)
        catalog.open()
        try:
            migrated_id = catalog.resolve_artifact_id("old-id")
            row = catalog.conn.execute("SELECT embedding FROM artifacts_vec WHERE id=?", (migrated_id,)).fetchone()
            assert row["embedding"] == embedding
            with pytest.raises(ValueError, match="unknown model identity"):
                catalog.search("", query_embedding=[0.9, 0.1, 0.0, 0.0])
            with pytest.raises(ValueError, match="unknown model identity"):
                catalog.validate_vector_state("configured-model", 4)
            with pytest.raises(ValueError, match="unknown model identity"):
                catalog.upsert_artifact(
                    artifact_id="new",
                    name="new.csv",
                    storage_uri="/new.csv",
                    embedding=[0.0, 1.0, 0.0, 0.0],
                )
        finally:
            catalog.close()

    def test_migration_with_vectors_reports_required_extra(self, tmp_path, monkeypatch):
        sqlite_vec = pytest.importorskip("sqlite_vec")
        path = tmp_path / "legacy-vectors-missing-extra.db"
        connection = sqlite3.connect(path)
        connection.enable_load_extension(True)
        sqlite_vec.load(connection)
        connection.enable_load_extension(False)
        connection.execute(
            """CREATE TABLE artifacts (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, storage_uri TEXT NOT NULL UNIQUE,
                description TEXT, domain TEXT, source_type TEXT, content_type TEXT,
                size_bytes INTEGER, indexed_at TEXT NOT NULL
            )"""
        )
        connection.execute("CREATE VIRTUAL TABLE artifacts_vec USING vec0(id TEXT PRIMARY KEY, embedding float[4])")
        connection.commit()
        connection.close()

        def missing_sqlite_vec(_name):
            raise ImportError("sqlite_vec is unavailable")

        monkeypatch.setattr(
            "agora_workbench.code_execution.data_access.catalog.db.import_module",
            missing_sqlite_vec,
        )
        with pytest.raises(
            RuntimeError,
            match="Migrating a catalog with existing vector embeddings requires sqlite-vec",
        ):
            CatalogDB(path, vec_dimensions=4).open()

    def test_migration_without_vector_table_succeeds(self, tmp_path):
        path = tmp_path / "legacy-no-vectors.db"
        connection = sqlite3.connect(path)
        connection.execute(
            """CREATE TABLE artifacts (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, storage_uri TEXT NOT NULL UNIQUE,
                description TEXT, domain TEXT, source_type TEXT, content_type TEXT,
                size_bytes INTEGER, indexed_at TEXT NOT NULL
            )"""
        )
        connection.execute(
            "INSERT INTO artifacts VALUES ('old-id', 'weather.csv', '/weather.csv', NULL, NULL, "
            "'local', 'text/csv', 12, '2026-01-01T00:00:00Z')"
        )
        connection.commit()
        connection.close()
        catalog = CatalogDB(path, vec_dimensions=4)
        catalog.open()
        try:
            assert catalog.get_artifact("old-id") is not None
        finally:
            catalog.close()

    def test_v0_export_is_deterministic_and_excludes_tombstones(self, db, tmp_path):
        db.upsert_artifact(
            artifact_id="live",
            source_id="source",
            logical_path="live.csv",
            name="live.csv",
            storage_uri="/live.csv",
            aliases=["old-live"],
        )
        db.upsert_artifact(
            artifact_id="deleted",
            source_id="source",
            logical_path="deleted.csv",
            name="deleted.csv",
            storage_uri="/deleted.csv",
        )
        db.delete_artifacts(["deleted"])
        destination = tmp_path / "v0.json"

        first = db.export_v0_json(destination)
        second = db.export_v0_json()

        assert first == second == destination.read_text()
        assert '"id": "old-live"' in first
        assert "deleted.csv" not in first
