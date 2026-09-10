"""Tests for catalog SQLite database."""

import sqlite3
import struct

import pytest

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
            assert catalog.search("", query_embedding=[0.9, 0.1, 0.0, 0.0])[0].id == migrated_id
        finally:
            catalog.close()

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
