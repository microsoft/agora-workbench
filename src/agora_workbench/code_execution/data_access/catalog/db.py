"""Versioned SQLite catalog with stable artifact identities and revisions."""

from __future__ import annotations

import hashlib
from importlib import import_module
import json
import logging
import re
import sqlite3
import struct
import threading
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from .identity import (
    ArtifactIdentityError,
    azure_uri_from_blob_name,
    canonicalize_azure_uri,
    logical_artifact_id,
    normalize_logical_path,
    sanitize_uri_for_display,
    split_alias,
)

LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 3
_VECTOR_EXTRA = "agora-workbench[catalog-vector]"
_VECTOR_TABLE_NAME = "artifacts_vec"
_VECTOR_DIMENSIONS_RE = re.compile(r"embedding\s+float\[(\d+)\]", re.IGNORECASE)
_SQL_IGNORED_RE = re.compile(r"'(?:''|[^'])*'|--[^\r\n]*|/\*.*?\*/", re.DOTALL)
_VECTOR_TABLE_REFERENCE_RE = re.compile(
    r'(?<![A-Za-z0-9_])(?:artifacts_vec|"artifacts_vec"|`artifacts_vec`|\[artifacts_vec\])(?![A-Za-z0-9_])',
    re.IGNORECASE,
)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS catalog_sources (
    source_id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    root_uri TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS catalog_source_refreshes (
    source_id TEXT PRIMARY KEY,
    attempt_generation INTEGER NOT NULL DEFAULT 0,
    successful_generation INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    artifact_count INTEGER,
    last_attempt_at TEXT NOT NULL,
    last_success_at TEXT,
    error TEXT,
    manifest_generation INTEGER,
    manifest_etag TEXT,
    FOREIGN KEY(source_id) REFERENCES catalog_sources(source_id)
);

CREATE TABLE IF NOT EXISTS catalog_vector_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    dimensions INTEGER NOT NULL,
    model_id TEXT
);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    logical_path TEXT NOT NULL,
    name TEXT NOT NULL,
    storage_uri TEXT NOT NULL,
    description TEXT,
    domain TEXT,
    source_type TEXT,
    content_type TEXT,
    size_bytes INTEGER,
    indexed_at TEXT NOT NULL,
    current_revision INTEGER NOT NULL DEFAULT 1,
    content_revision TEXT NOT NULL,
    metadata_revision TEXT NOT NULL,
    checksum_sha256 TEXT,
    deleted_at TEXT,
    UNIQUE(source_id, logical_path),
    FOREIGN KEY(source_id) REFERENCES catalog_sources(source_id)
);

CREATE TABLE IF NOT EXISTS artifact_revisions (
    artifact_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    source_id TEXT NOT NULL,
    logical_path TEXT NOT NULL,
    storage_uri TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    domain TEXT,
    source_type TEXT,
    content_type TEXT,
    size_bytes INTEGER,
    indexed_at TEXT NOT NULL,
    content_revision TEXT NOT NULL,
    metadata_revision TEXT NOT NULL,
    checksum_sha256 TEXT,
    deleted_at TEXT,
    PRIMARY KEY(artifact_id, revision),
    FOREIGN KEY(artifact_id) REFERENCES artifacts(id)
);

CREATE TABLE IF NOT EXISTS artifact_aliases (
    namespace TEXT NOT NULL,
    alias TEXT NOT NULL,
    source_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(source_id, namespace, alias),
    FOREIGN KEY(artifact_id) REFERENCES artifacts(id)
);

CREATE INDEX IF NOT EXISTS artifact_aliases_target_idx
ON artifact_aliases(source_id, artifact_id);

CREATE INDEX IF NOT EXISTS artifacts_live_domain_idx
ON artifacts(domain) WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS artifacts_live_source_type_idx
ON artifacts(source_type) WHERE deleted_at IS NULL;

CREATE VIRTUAL TABLE IF NOT EXISTS artifacts_fts USING fts5(
    name, description, domain,
    content='artifacts', content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS artifacts_ai AFTER INSERT ON artifacts
WHEN new.deleted_at IS NULL BEGIN
    INSERT INTO artifacts_fts(rowid, name, description, domain)
    VALUES (new.rowid, new.name, new.description, new.domain);
END;

CREATE TRIGGER IF NOT EXISTS artifacts_ad AFTER DELETE ON artifacts
WHEN old.deleted_at IS NULL BEGIN
    INSERT INTO artifacts_fts(artifacts_fts, rowid, name, description, domain)
    VALUES ('delete', old.rowid, old.name, old.description, old.domain);
END;

CREATE TRIGGER IF NOT EXISTS artifacts_au AFTER UPDATE ON artifacts BEGIN
    INSERT INTO artifacts_fts(artifacts_fts, rowid, name, description, domain)
    SELECT 'delete', old.rowid, old.name, old.description, old.domain
    WHERE old.deleted_at IS NULL;
    INSERT INTO artifacts_fts(rowid, name, description, domain)
    SELECT new.rowid, new.name, new.description, new.domain
    WHERE new.deleted_at IS NULL;
END;
"""


@dataclass
class ArtifactRecord:
    """One current or historical artifact revision."""

    id: str
    name: str
    storage_uri: str
    source_id: str = "legacy"
    logical_path: str = ""
    description: Optional[str] = None
    domain: Optional[str] = None
    source_type: Optional[str] = None
    content_type: Optional[str] = None
    size_bytes: Optional[int] = None
    indexed_at: Optional[str] = None
    current_revision: int = 1
    content_revision: str = ""
    metadata_revision: str = ""
    checksum_sha256: Optional[str] = None
    deleted_at: Optional[str] = None
    score: Optional[float] = None

    @property
    def revision(self) -> int:
        """Compatibility-neutral revision number."""
        return self.current_revision

    def to_dict(self) -> dict:
        """Convert to a public dictionary, excluding absent and internal fields."""
        result = {}
        for field in (
            "id",
            "source_id",
            "logical_path",
            "name",
            "storage_uri",
            "description",
            "domain",
            "source_type",
            "content_type",
            "size_bytes",
            "indexed_at",
            "current_revision",
            "content_revision",
            "metadata_revision",
            "checksum_sha256",
            "deleted_at",
        ):
            value = getattr(self, field)
            if value is not None:
                result[field] = value
        if self.score is not None:
            result["score"] = self.score
        return result


@dataclass(frozen=True)
class SourceRefreshState:
    """Most recent attempted and successful refresh state for one source."""

    source_id: str
    attempt_generation: int
    successful_generation: int
    status: str
    artifact_count: int | None
    last_attempt_at: str
    last_success_at: str | None
    error: str | None
    manifest_generation: int | None = None
    manifest_etag: str | None = None


def artifact_id_from_uri(uri: str) -> str:
    """Return the legacy URI-derived artifact ID.

    New catalog records use persisted logical IDs. This helper is retained for
    importing and resolving IDs written by earlier schema versions.
    """
    return hashlib.sha256(uri.encode()).hexdigest()[:16]


def _serialize_vector(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _references_vector_table(sql: str) -> bool:
    """Return whether SQL references the vector table outside literals/comments."""
    searchable_sql = _SQL_IGNORED_RE.sub(" ", sql)
    return _VECTOR_TABLE_REFERENCE_RE.search(searchable_sql) is not None


def _digest(parts: object) -> str:
    payload = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def _canonical_storage_alias(uri: str) -> str:
    parsed = urlsplit(uri)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    is_azure = scheme in {"az", "abfss"} or (
        scheme in {"http", "https"}
        and (host.endswith(".blob.core.windows.net") or host.endswith(".dfs.core.windows.net"))
    )
    if not is_azure:
        return uri

    literal_query = bool(parsed.query) and "=" not in parsed.query
    literal_fragment = bool(parsed.fragment) and "=" not in parsed.fragment
    if scheme == "az" and (literal_query or literal_fragment):
        literal_uri = CatalogDB._literal_legacy_az_uri(uri)
        if literal_uri is not None:
            return literal_uri

    try:
        return canonicalize_azure_uri(uri)
    except ArtifactIdentityError:
        return sanitize_uri_for_display(uri)


def _record_from_row(row: sqlite3.Row) -> ArtifactRecord:
    values = {key: row[key] for key in row.keys()}
    if "revision" in values:
        values["current_revision"] = values.pop("revision")
    return ArtifactRecord(**values)


class CatalogDB:
    """SQLite catalog with durable identity, history, FTS5, and optional vectors."""

    def __init__(self, db_path: str | Path = ":memory:", vec_dimensions: int | None = 768):
        if vec_dimensions is not None and vec_dimensions <= 0:
            raise ValueError("vec_dimensions must be greater than zero")
        self._db_path = str(db_path)
        self._vec_dimensions = vec_dimensions
        self._conn: Optional[sqlite3.Connection] = None
        self._vector_loaded = False
        self._write_lock = threading.RLock()

    def open(self) -> None:
        """Open the database and migrate known older schemas atomically."""
        connection = sqlite3.connect(self._db_path, timeout=5.0, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            connection.close()
            raise RuntimeError(
                f"Catalog schema version {version} is newer than supported version {SCHEMA_VERSION}; "
                "the database was not modified."
            )
        if self._db_path != ":memory:":
            try:
                journal_mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
                if journal_mode.lower() != "wal":
                    LOGGER.warning("WAL mode is unavailable for the catalog database")
            except sqlite3.DatabaseError:
                LOGGER.warning("WAL mode is unavailable for the catalog database")

        self._conn = connection
        try:
            self._probe_fts5()
            if version < 2 and self._has_legacy_schema():
                self._migrate_legacy_schema()
            else:
                connection.executescript(f"BEGIN IMMEDIATE;\n{_SCHEMA_SQL}")
                try:
                    refresh_columns = {
                        row["name"] for row in connection.execute("PRAGMA table_info(catalog_source_refreshes)")
                    }
                    if "manifest_generation" not in refresh_columns:
                        connection.execute(
                            "ALTER TABLE catalog_source_refreshes ADD COLUMN manifest_generation INTEGER"
                        )
                    if "manifest_etag" not in refresh_columns:
                        connection.execute("ALTER TABLE catalog_source_refreshes ADD COLUMN manifest_etag TEXT")
                    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
        except Exception:
            connection.close()
            self._conn = None
            raise

    def _has_legacy_schema(self) -> bool:
        row = self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='artifacts'").fetchone()
        if row is None:
            return False
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(artifacts)")}
        return "source_id" not in columns

    def _migrate_legacy_schema(self) -> None:
        """Migrate schema v0 records while retaining old IDs as aliases."""
        legacy_rows = self.conn.execute("SELECT * FROM artifacts ORDER BY rowid").fetchall()
        has_vectors = (
            self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='artifacts_vec'").fetchone()
            is not None
        )
        if has_vectors:
            self._ensure_vector_capability(
                self.conn,
                create_table=False,
                operation="Migrating a catalog with existing vector embeddings",
            )
        legacy_vectors = (
            {row["id"]: row["embedding"] for row in self.conn.execute("SELECT id, embedding FROM artifacts_vec")}
            if has_vectors
            else {}
        )
        self.conn.executescript(
            f"""BEGIN IMMEDIATE;
            DROP TRIGGER IF EXISTS artifacts_ai;
            DROP TRIGGER IF EXISTS artifacts_ad;
            DROP TRIGGER IF EXISTS artifacts_au;
            DROP TABLE IF EXISTS artifacts_fts;
            DROP TABLE IF EXISTS artifacts_vec;
            ALTER TABLE artifacts RENAME TO artifacts_v0;
            {_SCHEMA_SQL}
            """
        )
        try:
            self._vector_loaded = False
            if has_vectors:
                self._ensure_vector_capability(self.conn)

            for row in legacy_rows:
                old_id = row["id"]
                source_type = row["source_type"] or "legacy"
                source_id = f"legacy-{source_type}"
                logical_path = f"imported/{old_id}/{row['name']}"
                artifact_id = logical_artifact_id(source_id, logical_path)
                storage_uri = _canonical_storage_alias(row["storage_uri"])
                metadata_revision = _digest(
                    [row["name"], row["description"], row["domain"], source_type, row["content_type"]]
                )
                content_revision = (
                    f"legacy-size:{row['size_bytes']}" if row["size_bytes"] is not None else "legacy:unknown"
                )
                self._ensure_source(source_id, source_type, None, row["indexed_at"])
                self._insert_artifact(
                    artifact_id=artifact_id,
                    source_id=source_id,
                    logical_path=logical_path,
                    name=row["name"],
                    storage_uri=storage_uri,
                    description=row["description"],
                    domain=row["domain"],
                    source_type=source_type,
                    content_type=row["content_type"],
                    size_bytes=row["size_bytes"],
                    indexed_at=row["indexed_at"],
                    content_revision=content_revision,
                    metadata_revision=metadata_revision,
                    checksum_sha256=None,
                )
                self.add_alias("artifact-id", old_id, source_id, artifact_id, commit=False)
                self.add_alias(source_type, old_id, source_id, artifact_id, commit=False)
                self.add_alias("storage-uri", storage_uri, source_id, artifact_id, commit=False)
                try:
                    canonical_uri = canonicalize_azure_uri(row["storage_uri"])
                except Exception:
                    canonical_uri = None
                if canonical_uri is not None:
                    canonical_id = artifact_id_from_uri(canonical_uri)
                    self.add_alias("artifact-id", canonical_id, source_id, artifact_id, commit=False)
                    self.add_alias("storage-uri", canonical_uri, source_id, artifact_id, commit=False)
                literal_uri = self._literal_legacy_az_uri(row["storage_uri"])
                if literal_uri is not None and literal_uri == storage_uri:
                    literal_id = artifact_id_from_uri(literal_uri)
                    self.add_alias("artifact-id", literal_id, source_id, artifact_id, commit=False)
                    self.add_alias("storage-uri", literal_uri, source_id, artifact_id, commit=False)
                embedding = legacy_vectors.get(old_id)
                if embedding is not None:
                    self.conn.execute(
                        "INSERT INTO artifacts_vec(id, embedding) VALUES (?, ?)",
                        (artifact_id, embedding),
                    )

            self.conn.execute("DROP TABLE artifacts_v0")
            self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    @staticmethod
    def _literal_legacy_az_uri(uri: str) -> str | None:
        """Interpret a raw v0 az:// object portion as literal text."""
        if not uri.lower().startswith("az://"):
            return None
        parts = uri[5:].split("/", 2)
        if len(parts) != 3:
            return None
        account, container, object_name = parts
        try:
            return azure_uri_from_blob_name(account, container, object_name)
        except Exception:
            return None

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
            self._vector_loaded = False

    @property
    def vec_dimensions(self) -> int | None:
        """Configured or inferred vector dimensions for this catalog."""
        return self._vec_dimensions

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Database not opened. Call open() first.")
        return self._conn

    @contextmanager
    def _write_transaction(self):
        """Serialize writers and roll back every catalog index surface together."""
        with self._write_lock:
            if self.conn.in_transaction:
                self.conn.execute("SAVEPOINT catalog_write")
                try:
                    yield
                except Exception:
                    self.conn.execute("ROLLBACK TO catalog_write")
                    self.conn.execute("RELEASE catalog_write")
                    raise
                else:
                    self.conn.execute("RELEASE catalog_write")
                return
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except Exception:
                self.conn.rollback()
                raise
            else:
                self.conn.commit()

    @contextmanager
    def _read_snapshot(self, *, vectors: bool = False):
        """Yield one committed read snapshot, serializing safely for in-memory databases."""
        if self._db_path == ":memory:":
            with self._write_lock:
                if vectors:
                    self._ensure_vector_capability(self.conn)
                yield self.conn
            return

        if vectors:
            with self._write_lock:
                self._ensure_vector_capability(self.conn)
        uri = f"{Path(self._db_path).resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        if vectors:
            self._ensure_vector_capability(connection, create_table=False)
        connection.execute("BEGIN")
        connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        try:
            yield connection
        finally:
            connection.rollback()
            connection.close()

    def execute_readonly(self, sql: str, max_rows: int = 100) -> list[dict]:
        """Execute a SELECT from one committed snapshot."""
        stripped = sql.strip().upper()
        write_keywords = ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "REPLACE")
        if any(stripped.startswith(keyword) for keyword in write_keywords):
            raise ValueError(f"Write operations are not permitted. Query starts with: {stripped.split()[0]}")
        with self._read_snapshot(vectors=_references_vector_table(sql)) as connection:
            previous_query_only = connection.execute("PRAGMA query_only").fetchone()[0]
            connection.execute("PRAGMA query_only = ON")
            try:
                cursor = connection.execute(sql)
                return [dict(row) for row in cursor.fetchmany(max_rows)]
            finally:
                connection.execute(f"PRAGMA query_only = {int(previous_query_only)}")

    def _ensure_source(self, source_id: str, source_type: str, root_uri: str | None, created_at: str) -> None:
        existing = self.conn.execute(
            "SELECT source_type, root_uri FROM catalog_sources WHERE source_id = ?", (source_id,)
        ).fetchone()
        if existing is not None and existing["source_type"] != source_type:
            raise ValueError(f"Source ID collision for {source_id!r}")
        self.conn.execute(
            """INSERT INTO catalog_sources(source_id, source_type, root_uri, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(source_id) DO UPDATE
               SET root_uri=COALESCE(excluded.root_uri, catalog_sources.root_uri)""",
            (source_id, source_type, root_uri, created_at),
        )

    def _insert_artifact(
        self,
        *,
        artifact_id: str,
        source_id: str,
        logical_path: str,
        name: str,
        storage_uri: str,
        description: str | None,
        domain: str | None,
        source_type: str | None,
        content_type: str | None,
        size_bytes: int | None,
        indexed_at: str,
        content_revision: str,
        metadata_revision: str,
        checksum_sha256: str | None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO artifacts(
                   id, source_id, logical_path, name, storage_uri, description, domain,
                   source_type, content_type, size_bytes, indexed_at, current_revision,
                   content_revision, metadata_revision, checksum_sha256, deleted_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, NULL)""",
            (
                artifact_id,
                source_id,
                logical_path,
                name,
                storage_uri,
                description,
                domain,
                source_type,
                content_type,
                size_bytes,
                indexed_at,
                content_revision,
                metadata_revision,
                checksum_sha256,
            ),
        )
        self._insert_revision(artifact_id)

    def _insert_revision(self, artifact_id: str) -> None:
        self.conn.execute(
            """INSERT INTO artifact_revisions(
                   artifact_id, revision, source_id, logical_path, storage_uri, name,
                   description, domain, source_type, content_type, size_bytes, indexed_at,
                   content_revision, metadata_revision, checksum_sha256, deleted_at
               )
               SELECT id, current_revision, source_id, logical_path, storage_uri, name,
                      description, domain, source_type, content_type, size_bytes, indexed_at,
                      content_revision, metadata_revision, checksum_sha256, deleted_at
               FROM artifacts WHERE id = ?""",
            (artifact_id,),
        )

    def upsert_artifact(
        self,
        artifact_id: str | None,
        name: str,
        storage_uri: str,
        description: Optional[str] = None,
        domain: Optional[str] = None,
        source_type: Optional[str] = None,
        content_type: Optional[str] = None,
        size_bytes: Optional[int] = None,
        indexed_at: Optional[str] = None,
        embedding: Optional[list[float]] = None,
        *,
        source_id: str = "legacy",
        logical_path: str | None = None,
        source_root: str | None = None,
        content_revision: str | None = None,
        metadata_revision: str | None = None,
        checksum_sha256: str | None = None,
        aliases: tuple[str, ...] | list[str] = (),
        _replace_embedding: bool = False,
        _allow_move: bool = False,
        _commit: bool = True,
    ) -> str:
        """Insert or update an artifact and return its persisted logical ID."""
        now = indexed_at or datetime.now(timezone.utc).isoformat()
        source_type = source_type or "legacy"
        if not source_id:
            raise ValueError("Source ID must be non-empty")
        if artifact_id == "":
            raise ValueError("Artifact ID must be non-empty")
        if _commit:
            with self._write_transaction():
                return self.upsert_artifact(
                    artifact_id,
                    name,
                    storage_uri,
                    description,
                    domain,
                    source_type,
                    content_type,
                    size_bytes,
                    indexed_at,
                    embedding,
                    source_id=source_id,
                    logical_path=logical_path,
                    source_root=source_root,
                    content_revision=content_revision,
                    metadata_revision=metadata_revision,
                    checksum_sha256=checksum_sha256,
                    aliases=aliases,
                    _replace_embedding=_replace_embedding,
                    _allow_move=_allow_move,
                    _commit=False,
                )
        if embedding is not None:
            self._validate_vector_dimensions(embedding, "artifact embedding")
            self._ensure_vector_capability(self.conn)
        existing_by_id = (
            self.conn.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone() if artifact_id else None
        )
        requested_logical_path = logical_path
        if existing_by_id is not None and requested_logical_path is None:
            source_id = existing_by_id["source_id"]
            requested_logical_path = existing_by_id["logical_path"]
        logical_path = normalize_logical_path(requested_logical_path or name)
        content_revision = content_revision or (
            f"sha256:{checksum_sha256}"
            if checksum_sha256 is not None
            else (f"size:{size_bytes}" if size_bytes is not None else "unknown")
        )
        metadata_revision = metadata_revision or _digest([name, description, domain, source_type, content_type])

        with nullcontext():
            self._ensure_source(source_id, source_type, source_root, now)
            existing_by_path = self.conn.execute(
                "SELECT * FROM artifacts WHERE source_id = ? AND logical_path = ?",
                (source_id, logical_path),
            ).fetchone()
            configured_alias = None
            if existing_by_id is not None:
                chosen_id = existing_by_id["id"]
            elif existing_by_path is not None:
                chosen_id = existing_by_path["id"]
                if artifact_id is not None and artifact_id != chosen_id:
                    configured_alias = artifact_id
            else:
                chosen_id = artifact_id or logical_artifact_id(source_id, logical_path)
            adopting_legacy = (
                existing_by_id is not None
                and existing_by_id["id"] == chosen_id
                and existing_by_id["source_id"].startswith("legacy-")
                and existing_by_id["logical_path"].startswith("imported/")
            )
            if existing_by_id is not None and existing_by_id["source_id"] != source_id and not adopting_legacy:
                raise ValueError(
                    f"Artifact ID {chosen_id!r} is already assigned to source {existing_by_id['source_id']!r}"
                )
            if (
                existing_by_id is not None
                and existing_by_id["source_id"] == source_id
                and existing_by_id["logical_path"] != logical_path
                and not adopting_legacy
                and not _allow_move
            ):
                raise ValueError(
                    f"Artifact ID {chosen_id!r} is already assigned to {source_id}:{existing_by_id['logical_path']}"
                )
            existing = existing_by_id or existing_by_path

            conflicting_alias = self.conn.execute(
                "SELECT artifact_id FROM artifact_aliases WHERE namespace='artifact-id' AND alias=?",
                (chosen_id,),
            ).fetchone()
            if conflicting_alias is not None and conflicting_alias["artifact_id"] != chosen_id:
                raise ValueError(f"Artifact ID collides with an existing alias: {chosen_id!r}")
            if ":" in chosen_id:
                namespace, alias = split_alias(chosen_id)
                qualified_alias = self.conn.execute(
                    "SELECT artifact_id FROM artifact_aliases WHERE namespace=? AND alias=?",
                    (namespace, alias),
                ).fetchone()
                if qualified_alias is not None and qualified_alias["artifact_id"] != chosen_id:
                    raise ValueError(f"Artifact ID collides with an existing alias: {chosen_id!r}")
            if existing_by_path is not None and existing_by_path["id"] != chosen_id:
                raise ValueError(
                    f"Logical path {source_id}:{logical_path} is already assigned to artifact {existing_by_path['id']}"
                )

            if existing is None:
                id_owner = self.conn.execute("SELECT source_id FROM artifacts WHERE id = ?", (chosen_id,)).fetchone()
                if id_owner is not None:
                    raise ValueError(f"Artifact ID collision: {chosen_id!r}")
                self._insert_artifact(
                    artifact_id=chosen_id,
                    source_id=source_id,
                    logical_path=logical_path,
                    name=name,
                    storage_uri=storage_uri,
                    description=description,
                    domain=domain,
                    source_type=source_type,
                    content_type=content_type,
                    size_bytes=size_bytes,
                    indexed_at=now,
                    content_revision=content_revision,
                    metadata_revision=metadata_revision,
                    checksum_sha256=checksum_sha256,
                )
            else:
                changed = any(
                    (
                        existing["storage_uri"] != storage_uri,
                        existing["source_id"] != source_id,
                        existing["logical_path"] != logical_path,
                        existing["name"] != name,
                        existing["description"] != description,
                        existing["domain"] != domain,
                        existing["source_type"] != source_type,
                        existing["content_type"] != content_type,
                        existing["size_bytes"] != size_bytes,
                        existing["content_revision"] != content_revision,
                        existing["metadata_revision"] != metadata_revision,
                        existing["checksum_sha256"] != checksum_sha256,
                        existing["deleted_at"] is not None,
                    )
                )
                if changed:
                    revision = existing["current_revision"] + 1
                    self.conn.execute(
                        """UPDATE artifacts SET
                               source_id=?, logical_path=?, name=?, storage_uri=?, description=?,
                               domain=?, source_type=?, content_type=?, size_bytes=?, indexed_at=?,
                               current_revision=?, content_revision=?, metadata_revision=?,
                               checksum_sha256=?, deleted_at=NULL
                           WHERE id=?""",
                        (
                            source_id,
                            logical_path,
                            name,
                            storage_uri,
                            description,
                            domain,
                            source_type,
                            content_type,
                            size_bytes,
                            now,
                            revision,
                            content_revision,
                            metadata_revision,
                            checksum_sha256,
                            chosen_id,
                        ),
                    )
                    if adopting_legacy:
                        self.conn.execute(
                            "UPDATE artifact_aliases SET source_id=? WHERE artifact_id=?",
                            (source_id, chosen_id),
                        )
                    self._insert_revision(chosen_id)

            for alias in aliases:
                namespace, value = split_alias(alias)
                self.add_alias(namespace, value, source_id, chosen_id, commit=False)
            if configured_alias is not None:
                namespace, value = split_alias(configured_alias)
                self.add_alias(namespace, value, source_id, chosen_id, commit=False)
            if _replace_embedding or embedding is not None:
                self._ensure_vector_capability(self.conn)
                self.conn.execute("DELETE FROM artifacts_vec WHERE id = ?", (chosen_id,))
            if embedding is not None:
                vector_state = self.conn.execute(
                    "SELECT model_id FROM catalog_vector_state WHERE singleton=1"
                ).fetchone()
                if vector_state["model_id"] is None:
                    vector_count = self.conn.execute("SELECT COUNT(*) FROM artifacts_vec").fetchone()[0]
                    if vector_count:
                        raise ValueError(
                            "Catalog contains vectors with unknown model identity; rebuild vectors before writing"
                        )
                    self.conn.execute("UPDATE catalog_vector_state SET model_id='manual' WHERE singleton=1")
                self.conn.execute(
                    "INSERT INTO artifacts_vec (id, embedding) VALUES (?, ?)",
                    (chosen_id, _serialize_vector(embedding)),
                )
        return chosen_id

    def upsert_artifacts_batch(self, artifacts: list[dict]) -> None:
        """Atomically upsert a batch of artifacts."""
        with self._write_transaction():
            for artifact in artifacts:
                self.upsert_artifact(**artifact, _commit=False)

    def apply_refresh_batch(
        self,
        artifacts: list[dict],
        stale_artifact_ids: list[str],
        source_results: list[dict],
        vector_model_id: str | None = None,
    ) -> None:
        """Apply one refresh generation atomically across rows, search indexes, and vectors."""
        with self._write_transaction():
            if vector_model_id is not None:
                self._ensure_vector_capability(self.conn)
                dimensions = self._vec_dimensions
                if dimensions is None:
                    raise ValueError("Catalog vector dimensions are unknown after vector capability initialization")
                self._validate_vector_state(self.conn, vector_model_id, dimensions)
                self.conn.execute(
                    "UPDATE catalog_vector_state SET model_id=? WHERE singleton=1",
                    (vector_model_id,),
                )
            for result in source_results:
                self._ensure_source(
                    result["source_id"],
                    result["source_type"],
                    result.get("root_uri"),
                    result["attempted_at"],
                )
            refresh_timestamp = source_results[0]["attempted_at"] if source_results else None
            self._delete_artifacts(stale_artifact_ids, deleted_at=refresh_timestamp)
            for artifact in artifacts:
                self.upsert_artifact(**artifact, _commit=False)
            for result in source_results:
                self._record_source_refresh(**result)

    def _record_source_refresh(
        self,
        *,
        source_id: str,
        source_type: str,
        attempted_at: str,
        succeeded: bool,
        artifact_count: int | None,
        error: str | None,
        root_uri: str | None = None,
        manifest_generation: int | None = None,
        manifest_etag: str | None = None,
    ) -> None:
        del source_type, root_uri
        existing = self.conn.execute(
            "SELECT attempt_generation, successful_generation, last_success_at, manifest_generation, manifest_etag "
            "FROM catalog_source_refreshes WHERE source_id=?",
            (source_id,),
        ).fetchone()
        attempt_generation = (existing["attempt_generation"] if existing else 0) + 1
        successful_generation = (
            attempt_generation if succeeded else (existing["successful_generation"] if existing else 0)
        )
        last_success_at = attempted_at if succeeded else (existing["last_success_at"] if existing else None)
        successful_manifest_generation = (
            manifest_generation if succeeded else (existing["manifest_generation"] if existing else None)
        )
        successful_manifest_etag = manifest_etag if succeeded else (existing["manifest_etag"] if existing else None)
        self.conn.execute(
            """INSERT INTO catalog_source_refreshes(
                   source_id, attempt_generation, successful_generation, status, artifact_count,
                   last_attempt_at, last_success_at, error, manifest_generation, manifest_etag
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(source_id) DO UPDATE SET
                   attempt_generation=excluded.attempt_generation,
                   successful_generation=excluded.successful_generation,
                   status=excluded.status,
                   artifact_count=excluded.artifact_count,
                   last_attempt_at=excluded.last_attempt_at,
                   last_success_at=excluded.last_success_at,
                   error=excluded.error,
                   manifest_generation=excluded.manifest_generation,
                   manifest_etag=excluded.manifest_etag""",
            (
                source_id,
                attempt_generation,
                successful_generation,
                "success" if succeeded else "error",
                artifact_count,
                attempted_at,
                last_success_at,
                error,
                successful_manifest_generation,
                successful_manifest_etag,
            ),
        )

    def get_source_refresh_state(self, source_id: str) -> SourceRefreshState | None:
        """Return the latest observable refresh state for a source."""
        with self._read_snapshot() as connection:
            row = connection.execute(
                "SELECT * FROM catalog_source_refreshes WHERE source_id=?", (source_id,)
            ).fetchone()
        return SourceRefreshState(**dict(row)) if row is not None else None

    def list_source_refresh_states(self) -> list[SourceRefreshState]:
        """Return latest refresh states in deterministic source order."""
        with self._read_snapshot() as connection:
            rows = connection.execute("SELECT * FROM catalog_source_refreshes ORDER BY source_id").fetchall()
        return [SourceRefreshState(**dict(row)) for row in rows]

    def validate_vector_state(self, model_id: str, dimensions: int) -> None:
        """Reject incompatible vector model/dimension reuse instead of mixing embeddings."""
        with self._write_lock:
            if self._vec_dimensions is None:
                self._vec_dimensions = dimensions
        with self._read_snapshot(vectors=True) as connection:
            self._validate_vector_state(connection, model_id, dimensions)

    def _validate_vector_state(self, connection: sqlite3.Connection, model_id: str, dimensions: int) -> None:
        if dimensions != self._vec_dimensions:
            raise ValueError(
                f"Embedding provider dimension mismatch: database expects {self._vec_dimensions}, got {dimensions}"
            )
        row = connection.execute("SELECT dimensions, model_id FROM catalog_vector_state WHERE singleton=1").fetchone()
        if row["dimensions"] != dimensions:
            raise ValueError(f"Catalog vector dimension mismatch: database uses {row['dimensions']}, got {dimensions}")
        vector_count = connection.execute("SELECT COUNT(*) FROM artifacts_vec").fetchone()[0]
        if row["model_id"] is None and vector_count:
            raise ValueError("Catalog contains vectors with unknown model identity; rebuild vectors before searching")
        if row["model_id"] not in (None, model_id):
            if vector_count:
                raise ValueError(
                    f"Catalog vector model changed from {row['model_id']!r} to {model_id!r}; "
                    "rebuild vectors before searching"
                )

    def has_vector(self, artifact_id: str) -> bool:
        """Return whether the current artifact has an indexed embedding."""
        return artifact_id not in self.missing_vectors([artifact_id])

    def missing_vectors(self, artifact_ids: list[str]) -> set[str]:
        """Return artifact IDs without vectors using one operation-scoped snapshot."""
        unique_ids = list(dict.fromkeys(artifact_ids))
        if not unique_ids:
            return set()
        present_ids: set[str] = set()
        with self._read_snapshot(vectors=True) as connection:
            for offset in range(0, len(unique_ids), 500):
                batch = unique_ids[offset : offset + 500]
                placeholders = ",".join("?" for _ in batch)
                rows = connection.execute(
                    f"SELECT id FROM artifacts_vec WHERE id IN ({placeholders})",
                    batch,
                ).fetchall()
                present_ids.update(row["id"] for row in rows)
        return set(unique_ids) - present_ids

    def add_alias(
        self,
        namespace: str,
        alias: str,
        source_id: str,
        artifact_id: str,
        *,
        commit: bool = True,
    ) -> None:
        """Add an unambiguous namespaced alias."""
        if commit:
            with self._write_transaction():
                self.add_alias(namespace, alias, source_id, artifact_id, commit=False)
            return
        if not namespace or not alias:
            raise ValueError("Alias namespace and value must be non-empty")
        canonical_candidates = (
            (alias, f"{namespace}:{alias}") if namespace == "artifact-id" else (f"{namespace}:{alias}",)
        )
        placeholders = ",".join("?" for _ in canonical_candidates)
        direct = self.conn.execute(
            f"SELECT id FROM artifacts WHERE id IN ({placeholders}) AND id != ?",
            (*canonical_candidates, artifact_id),
        ).fetchone()
        if direct is not None:
            raise ValueError(f"Alias collides with canonical artifact ID: {direct['id']!r}")
        existing = self.conn.execute(
            "SELECT artifact_id FROM artifact_aliases WHERE source_id=? AND namespace=? AND alias=?",
            (source_id, namespace, alias),
        ).fetchone()
        if existing is not None and existing["artifact_id"] != artifact_id:
            raise ValueError(f"Alias collision in source {source_id!r}: {namespace}:{alias}")
        self.conn.execute(
            """INSERT OR IGNORE INTO artifact_aliases(namespace, alias, source_id, artifact_id, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (namespace, alias, source_id, artifact_id, datetime.now(timezone.utc).isoformat()),
        )

    def resolve_artifact_id(self, value: str, source_id: str | None = None) -> str | None:
        """Resolve a canonical ID or namespaced compatibility alias."""
        with self._read_snapshot() as connection:
            return self._resolve_artifact_id(connection, value, source_id)

    @staticmethod
    def _resolve_artifact_id(connection: sqlite3.Connection, value: str, source_id: str | None = None) -> str | None:
        direct = connection.execute(
            "SELECT id FROM artifacts WHERE id=? AND (? IS NULL OR source_id=?)",
            (value, source_id, source_id),
        ).fetchone()
        if direct is not None:
            return direct["id"]
        namespace, alias = split_alias(value)
        if namespace == "storage-uri":
            alias = _canonical_storage_alias(alias)
        rows = connection.execute(
            """SELECT DISTINCT artifact_id FROM artifact_aliases
               WHERE namespace=? AND alias=? AND (? IS NULL OR source_id=?)""",
            (namespace, alias, source_id, source_id),
        ).fetchall()
        if len(rows) > 1:
            raise ValueError(f"Ambiguous artifact alias: {namespace}:{alias}; specify source_id")
        return rows[0]["artifact_id"] if rows else None

    def resolve_scan_alias(self, value: str, source_id: str) -> str | None:
        """Resolve a scan alias within its source or against one unadopted migration record."""
        with self._read_snapshot() as connection:
            resolved = self._resolve_artifact_id(connection, value, source_id)
            if resolved is not None:
                return resolved
            namespace, alias = split_alias(value)
            rows = connection.execute(
                """SELECT DISTINCT a.id FROM artifact_aliases aa
                   JOIN artifacts a ON a.id=aa.artifact_id
                   WHERE aa.namespace=? AND aa.alias=?
                     AND a.source_id LIKE 'legacy-%'
                     AND a.logical_path LIKE 'imported/%'""",
                (namespace, alias),
            ).fetchall()
            if len(rows) > 1:
                raise ValueError(f"Ambiguous migrated artifact alias: {namespace}:{alias}")
            return rows[0]["id"] if rows else None

    def export_v0_json(self, destination: str | Path | None = None) -> str:
        """Export current live artifacts as a deterministic v0-compatible JSON array."""
        with self._read_snapshot() as connection:
            rows = connection.execute(
                """SELECT id, name, storage_uri, description, domain, source_type,
                          content_type, size_bytes, indexed_at
                   FROM artifacts WHERE deleted_at IS NULL ORDER BY source_id, logical_path, id"""
            ).fetchall()
            records = []
            for row in rows:
                legacy_id = artifact_id_from_uri(row["storage_uri"])
                legacy_alias = connection.execute(
                    """SELECT alias FROM artifact_aliases
                       WHERE artifact_id=? AND namespace='artifact-id'
                       ORDER BY CASE WHEN alias=? THEN 0 ELSE 1 END, alias LIMIT 1""",
                    (row["id"], legacy_id),
                ).fetchone()
                record = dict(row)
                record["id"] = legacy_alias["alias"] if legacy_alias is not None else row["id"]
                records.append(record)
        payload = json.dumps(records, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if destination is not None:
            Path(destination).write_text(payload, encoding="utf-8")
        return payload

    def get_artifact(
        self,
        artifact_id: str,
        *,
        source_id: str | None = None,
        revision: int | None = None,
        include_deleted: bool = False,
    ) -> Optional[ArtifactRecord]:
        """Retrieve a current or revision-qualified record by ID or alias."""
        with self._read_snapshot() as connection:
            resolved_id = self._resolve_artifact_id(connection, artifact_id, source_id)
            if resolved_id is None:
                return None
            if revision is None:
                row = connection.execute("SELECT * FROM artifacts WHERE id = ?", (resolved_id,)).fetchone()
            else:
                row = connection.execute(
                    """SELECT artifact_id AS id, source_id, logical_path, name, storage_uri,
                              description, domain, source_type, content_type, size_bytes,
                              indexed_at, revision, content_revision, metadata_revision,
                              checksum_sha256, deleted_at
                       FROM artifact_revisions WHERE artifact_id=? AND revision=?""",
                    (resolved_id, revision),
                ).fetchone()
            if row is None or (row["deleted_at"] is not None and not include_deleted):
                return None
            return _record_from_row(row)

    def find_by_source_path(
        self, source_id: str, logical_path: str, *, include_deleted: bool = False
    ) -> Optional[ArtifactRecord]:
        with self._read_snapshot() as connection:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE source_id=? AND logical_path=?",
                (source_id, normalize_logical_path(logical_path)),
            ).fetchone()
            if row is None or (row["deleted_at"] is not None and not include_deleted):
                return None
            return _record_from_row(row)

    def list_revisions(self, artifact_id: str, *, source_id: str | None = None) -> list[ArtifactRecord]:
        """Return all retained revisions, including deletion tombstones."""
        with self._read_snapshot() as connection:
            resolved_id = self._resolve_artifact_id(connection, artifact_id, source_id)
            if resolved_id is None:
                return []
            rows = connection.execute(
                """SELECT artifact_id AS id, source_id, logical_path, name, storage_uri,
                          description, domain, source_type, content_type, size_bytes,
                          indexed_at, revision, content_revision, metadata_revision,
                          checksum_sha256, deleted_at
                   FROM artifact_revisions WHERE artifact_id=? ORDER BY revision""",
                (resolved_id,),
            ).fetchall()
            return [_record_from_row(row) for row in rows]

    def get_existing_uris(self, source_id: str | None = None) -> set[str]:
        with self._read_snapshot() as connection:
            if source_id is None:
                rows = connection.execute("SELECT storage_uri FROM artifacts WHERE deleted_at IS NULL").fetchall()
            else:
                rows = connection.execute(
                    "SELECT storage_uri FROM artifacts WHERE source_id=? AND deleted_at IS NULL", (source_id,)
                ).fetchall()
            return {row[0] for row in rows}

    def current_paths(self, source_id: str) -> dict[str, str]:
        with self._read_snapshot() as connection:
            rows = connection.execute(
                "SELECT logical_path, id FROM artifacts WHERE source_id=? AND deleted_at IS NULL", (source_id,)
            ).fetchall()
            return {row["logical_path"]: row["id"] for row in rows}

    def records_by_source_path(self, source_id: str, *, include_deleted: bool = False) -> dict[str, ArtifactRecord]:
        """Return one source's records keyed by normalized logical path."""
        deleted_filter = "" if include_deleted else " AND deleted_at IS NULL"
        with self._read_snapshot() as connection:
            rows = connection.execute(
                f"SELECT * FROM artifacts WHERE source_id=?{deleted_filter}",
                (source_id,),
            ).fetchall()
            return {row["logical_path"]: _record_from_row(row) for row in rows}

    def retained_artifact_ids_by_source_path(self, source_id: str) -> dict[str, set[str]]:
        """Return current and revision artifact IDs grouped by retained logical path."""
        with self._read_snapshot() as connection:
            rows = connection.execute(
                """SELECT logical_path, id AS artifact_id
                   FROM artifacts
                   WHERE source_id=?
                   UNION
                   SELECT logical_path, artifact_id
                   FROM artifact_revisions
                   WHERE source_id=?""",
                (source_id, source_id),
            ).fetchall()
        retained: dict[str, set[str]] = {}
        for row in rows:
            retained.setdefault(row["logical_path"], set()).add(row["artifact_id"])
        return retained

    def delete_artifacts(self, artifact_ids: list[str], *, deleted_at: str | None = None) -> None:
        """Create tombstone revisions; history is retained until explicit purge."""
        with self._write_transaction():
            self._delete_artifacts(artifact_ids, deleted_at=deleted_at)

    def _delete_artifacts(self, artifact_ids: list[str], *, deleted_at: str | None) -> None:
        timestamp = deleted_at or datetime.now(timezone.utc).isoformat()
        has_vector_table = self._vector_loaded or self._has_vector_table(self.conn)
        if has_vector_table:
            self._ensure_vector_capability(self.conn, create_table=False)
        for value in artifact_ids:
            artifact_id = self._resolve_artifact_id(self.conn, value)
            if artifact_id is None:
                continue
            row = self.conn.execute(
                "SELECT current_revision, deleted_at FROM artifacts WHERE id=?", (artifact_id,)
            ).fetchone()
            if row["deleted_at"] is not None:
                continue
            self.conn.execute(
                "UPDATE artifacts SET current_revision=?, deleted_at=?, indexed_at=? WHERE id=?",
                (row["current_revision"] + 1, timestamp, timestamp, artifact_id),
            )
            self._insert_revision(artifact_id)
            if has_vector_table:
                self.conn.execute("DELETE FROM artifacts_vec WHERE id = ?", (artifact_id,))

    def purge_deleted(self, before: str) -> int:
        """Permanently remove tombstones older than *before* and all their revisions."""
        with self._write_transaction():
            rows = self.conn.execute(
                """SELECT id FROM artifacts
                   WHERE deleted_at IS NOT NULL
                     AND julianday(deleted_at) < julianday(?)""",
                (before,),
            ).fetchall()
            ids = [row["id"] for row in rows]
            if not ids:
                return 0
            placeholders = ",".join("?" for _ in ids)
            has_vector_table = self._vector_loaded or self._has_vector_table(self.conn)
            if has_vector_table:
                self._ensure_vector_capability(self.conn, create_table=False)
                self.conn.execute(f"DELETE FROM artifacts_vec WHERE id IN ({placeholders})", ids)
            self.conn.execute(f"DELETE FROM artifact_aliases WHERE artifact_id IN ({placeholders})", ids)
            self.conn.execute(f"DELETE FROM artifact_revisions WHERE artifact_id IN ({placeholders})", ids)
            self.conn.execute(f"DELETE FROM artifacts WHERE id IN ({placeholders})", ids)
        return len(ids)

    def list_domains(self) -> list[str]:
        with self._read_snapshot() as connection:
            rows = connection.execute(
                "SELECT DISTINCT domain FROM artifacts WHERE domain IS NOT NULL AND deleted_at IS NULL ORDER BY domain"
            ).fetchall()
            return [row[0] for row in rows]

    def list_artifacts(
        self,
        *,
        source_ids: tuple[str, ...] = (),
        domain: str | None = None,
        source_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ArtifactRecord]:
        """List current artifacts deterministically for public provider adapters."""
        conditions = ["deleted_at IS NULL"]
        params: list[object] = []
        if source_ids:
            placeholders = ",".join("?" for _ in source_ids)
            conditions.append(f"source_id IN ({placeholders})")
            params.extend(source_ids)
        if domain:
            conditions.append("domain = ?")
            params.append(domain)
        if source_type:
            conditions.append("source_type = ?")
            params.append(source_type)
        with self._read_snapshot() as connection:
            rows = connection.execute(
                f"""SELECT * FROM artifacts
                    WHERE {" AND ".join(conditions)}
                    ORDER BY source_id, logical_path, id LIMIT ? OFFSET ?""",
                (*params, max(0, limit), max(0, offset)),
            ).fetchall()
        return [_record_from_row(row) for row in rows]

    def search(
        self,
        query: str,
        query_embedding: Optional[list[float]] = None,
        domain: Optional[str] = None,
        source_type: Optional[str] = None,
        top: int = 10,
        hybrid_alpha: float = 0.5,
        source_ids: tuple[str, ...] = (),
        offset: int = 0,
    ) -> list[ArtifactRecord]:
        top = max(0, min(top, 1000))
        offset = max(0, offset)
        if top == 0:
            return []
        domain = domain or None
        source_type = source_type or None
        stripped_query = query.strip()
        if query_embedding is not None:
            self._validate_vector_dimensions(query_embedding, "query embedding")
        with self._read_snapshot(vectors=query_embedding is not None) as connection:
            if not stripped_query and query_embedding is None:
                conditions = ["deleted_at IS NULL"]
                params: list[object] = []
                if domain:
                    conditions.append("domain = ?")
                    params.append(domain)
                if source_type:
                    conditions.append("source_type = ?")
                    params.append(source_type)
                if source_ids:
                    placeholders = ",".join("?" for _ in source_ids)
                    conditions.append(f"source_id IN ({placeholders})")
                    params.extend(source_ids)
                rows = connection.execute(
                    f"""SELECT * FROM artifacts
                        WHERE {" AND ".join(conditions)}
                        ORDER BY source_id, logical_path, id LIMIT ? OFFSET ?""",
                    (*params, top, offset),
                ).fetchall()
                return [_record_from_row(row) for row in rows]

            fts_scores: dict[str, float] = {}
            vec_scores: dict[str, float] = {}
            candidate_keys: dict[str, tuple[str, str]] = {}
            required_candidates = offset + top
            candidate_limit = max(required_candidates * 3, required_candidates)
            fts_query = self._literal_fts_query(stripped_query)
            if fts_query:
                conditions = ["artifacts_fts MATCH ?", "a.deleted_at IS NULL"]
                params: list[object] = [fts_query]
                if domain:
                    conditions.append("a.domain = ?")
                    params.append(domain)
                if source_type:
                    conditions.append("a.source_type = ?")
                    params.append(source_type)
                if source_ids:
                    placeholders = ",".join("?" for _ in source_ids)
                    conditions.append(f"a.source_id IN ({placeholders})")
                    params.extend(source_ids)
                fts_rows = connection.execute(
                    f"""SELECT a.id, a.source_id, a.logical_path, rank FROM artifacts_fts fts
                       JOIN artifacts a ON a.rowid = fts.rowid
                       WHERE {" AND ".join(conditions)}
                       ORDER BY rank, a.source_id, a.logical_path, a.id LIMIT ?""",
                    (*params, candidate_limit),
                ).fetchall()
                if fts_rows:
                    min_rank = min(row["rank"] for row in fts_rows)
                    max_rank = max(row["rank"] for row in fts_rows)
                    rank_range = max_rank - min_rank if max_rank != min_rank else 1.0
                    for row in fts_rows:
                        fts_scores[row["id"]] = 1.0 - (row["rank"] - min_rank) / rank_range
                        candidate_keys[row["id"]] = (row["source_id"], row["logical_path"])
            if query_embedding is not None:
                has_vectors = connection.execute("SELECT 1 FROM artifacts_vec LIMIT 1").fetchone() is not None
                if has_vectors:
                    vector_state = connection.execute(
                        "SELECT model_id FROM catalog_vector_state WHERE singleton=1"
                    ).fetchone()
                    if vector_state["model_id"] is None:
                        raise ValueError(
                            "Catalog contains vectors with unknown model identity; rebuild vectors before searching"
                        )
                serialized_embedding = _serialize_vector(query_embedding)
                if domain or source_type or source_ids:
                    conditions = ["a.deleted_at IS NULL"]
                    params = [serialized_embedding]
                    if domain:
                        conditions.append("a.domain = ?")
                        params.append(domain)
                    if source_type:
                        conditions.append("a.source_type = ?")
                        params.append(source_type)
                    if source_ids:
                        placeholders = ",".join("?" for _ in source_ids)
                        conditions.append(f"a.source_id IN ({placeholders})")
                        params.extend(source_ids)
                    vec_rows = connection.execute(
                        f"""SELECT a.id, a.source_id, a.logical_path,
                                   vec_distance_L2(v.embedding, ?) AS distance
                            FROM artifacts a
                            CROSS JOIN artifacts_vec v ON v.id = a.id
                            WHERE {" AND ".join(conditions)}
                            ORDER BY distance, a.source_id, a.logical_path, a.id
                            LIMIT ?""",
                        (*params, candidate_limit),
                    ).fetchall()
                else:
                    vec_rows = connection.execute(
                        """SELECT a.id, a.source_id, a.logical_path, v.distance
                           FROM artifacts_vec v
                           JOIN artifacts a ON a.id = v.id
                           WHERE v.embedding MATCH ? AND v.k = ? AND a.deleted_at IS NULL
                           ORDER BY v.distance, a.source_id, a.logical_path, a.id
                           LIMIT ?""",
                        (serialized_embedding, candidate_limit, candidate_limit),
                    ).fetchall()
                for row in vec_rows:
                    vec_scores[row["id"]] = 1.0 - row["distance"]
                    candidate_keys[row["id"]] = (row["source_id"], row["logical_path"])

            all_ids = set(fts_scores) | set(vec_scores)
            if not all_ids:
                return []
            scored = sorted(
                (
                    (
                        artifact_id,
                        hybrid_alpha * fts_scores.get(artifact_id, 0.0)
                        + (1.0 - hybrid_alpha) * vec_scores.get(artifact_id, 0.0),
                    )
                    for artifact_id in all_ids
                ),
                key=lambda item: (
                    -item[1],
                    candidate_keys[item[0]][0],
                    candidate_keys[item[0]][1],
                    item[0],
                ),
            )
            selected = scored[offset : offset + top]
            placeholders = ",".join("?" for _ in selected)
            rows = connection.execute(
                f"SELECT * FROM artifacts WHERE id IN ({placeholders})",
                [artifact_id for artifact_id, _score in selected],
            ).fetchall()
            records = {row["id"]: _record_from_row(row) for row in rows}
            results: list[ArtifactRecord] = []
            for artifact_id, score in selected:
                record = records[artifact_id]
                record.score = score
                results.append(record)
            return results

    @staticmethod
    def _literal_fts_query(query: str) -> str | None:
        """Convert user text to a literal-token FTS expression."""
        tokens = re.findall(r"\w+", query, flags=re.UNICODE)
        if not tokens:
            return None
        return " AND ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)

    def _probe_fts5(self) -> None:
        """Verify that the active Python SQLite build supports FTS5."""
        try:
            self.conn.execute("CREATE VIRTUAL TABLE temp.__agora_fts5_probe USING fts5(value)")
            self.conn.execute("DROP TABLE temp.__agora_fts5_probe")
        except sqlite3.OperationalError as exc:
            raise RuntimeError(
                "Catalog keyword search requires SQLite with FTS5 support. "
                "Install a Python build whose sqlite3 module includes FTS5."
            ) from exc

    def _ensure_vector_capability(
        self,
        conn: sqlite3.Connection,
        *,
        create_table: bool = True,
        operation: str = "Catalog vector search",
    ) -> None:
        """Load sqlite-vec and validate or create vector storage on demand."""
        if conn is self._conn and self._vector_loaded:
            return

        had_active_transaction = conn.in_transaction
        try:
            sqlite_vec = import_module("sqlite_vec")
        except ImportError as exc:
            raise RuntimeError(
                f"{operation} requires sqlite-vec. Install the '{_VECTOR_EXTRA}' extra and reopen the catalog."
            ) from exc

        try:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
        except (AttributeError, sqlite3.Error) as exc:
            raise RuntimeError(
                "Catalog vector search could not load the sqlite-vec extension. "
                f"Reinstall the '{_VECTOR_EXTRA}' extra for this Python platform."
            ) from exc
        finally:
            try:
                conn.enable_load_extension(False)
            except (AttributeError, sqlite3.Error):
                pass

        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (_VECTOR_TABLE_NAME,),
        ).fetchone()
        if row is None:
            if not create_table:
                raise RuntimeError("Catalog vector table does not exist. Index artifacts with embeddings first.")
            if self._vec_dimensions is None:
                raise ValueError(
                    "Catalog vector dimensions are unknown. Supply vec_dimensions or write an embedding first."
                )
            conn.execute(
                f"CREATE VIRTUAL TABLE {_VECTOR_TABLE_NAME} USING vec0("
                f"  id TEXT PRIMARY KEY, embedding float[{self._vec_dimensions}]"
                f")"
            )
        else:
            table_sql = row[0] or ""
            match = _VECTOR_DIMENSIONS_RE.search(table_sql)
            if match is None:
                raise RuntimeError("Could not determine the dimensions of the existing catalog vector table.")
            stored_dimensions = int(match.group(1))
            if self._vec_dimensions is None:
                self._vec_dimensions = stored_dimensions
            elif stored_dimensions != self._vec_dimensions:
                raise ValueError(
                    "Catalog vector dimension mismatch: "
                    f"database uses {stored_dimensions}, but CatalogDB is configured for {self._vec_dimensions}."
                )

        has_state_table = (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'catalog_vector_state'"
            ).fetchone()
            is not None
        )
        if has_state_table:
            state = conn.execute("SELECT dimensions FROM catalog_vector_state WHERE singleton=1").fetchone()
            if state is None:
                if not create_table:
                    raise RuntimeError("Catalog vector model state is missing; reopen the catalog for vector indexing.")
                conn.execute(
                    "INSERT INTO catalog_vector_state(singleton, dimensions, model_id) VALUES (1, ?, NULL)",
                    (self._vec_dimensions,),
                )
            elif state["dimensions"] != self._vec_dimensions:
                raise ValueError(
                    f"Catalog vector dimension mismatch: database uses {state['dimensions']}, "
                    f"but CatalogDB is configured for {self._vec_dimensions}."
                )

        if create_table:
            conn.execute(f"DELETE FROM {_VECTOR_TABLE_NAME} WHERE id NOT IN (SELECT id FROM artifacts)")
            if not had_active_transaction:
                conn.commit()
        if conn is self._conn and not had_active_transaction:
            self._vector_loaded = True

    def _validate_vector_dimensions(self, vector: list[float], label: str) -> None:
        if not vector:
            raise ValueError(f"{label.capitalize()} must not be empty.")
        if self._vec_dimensions is None:
            self._vec_dimensions = len(vector)
            return
        if len(vector) != self._vec_dimensions:
            if label == "artifact embedding":
                raise ValueError(
                    f"Embedding dimension mismatch for artifact: expected {self._vec_dimensions}, got {len(vector)}."
                )
            raise ValueError(
                f"{label.capitalize()} dimension mismatch: expected {self._vec_dimensions}, got {len(vector)}."
            )

    @staticmethod
    def _has_vector_table(conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (_VECTOR_TABLE_NAME,),
        ).fetchone()
        return row is not None
