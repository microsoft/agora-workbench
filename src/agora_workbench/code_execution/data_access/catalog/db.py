"""SQLite catalog database with FTS5 keyword search and optional vectors."""

from __future__ import annotations

import hashlib
from importlib import import_module
import logging
import re
import sqlite3
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

LOGGER = logging.getLogger(__name__)

_VECTOR_EXTRA = "agora-workbench[catalog-vector]"
_VECTOR_TABLE_NAME = "artifacts_vec"
_VECTOR_DIMENSIONS_RE = re.compile(r"embedding\s+float\[(\d+)\]", re.IGNORECASE)
_SQL_IGNORED_RE = re.compile(r"'(?:''|[^'])*'|--[^\r\n]*|/\*.*?\*/", re.DOTALL)
_VECTOR_TABLE_REFERENCE_RE = re.compile(
    r'(?<![A-Za-z0-9_])(?:artifacts_vec|"artifacts_vec"|`artifacts_vec`|\[artifacts_vec\])(?![A-Za-z0-9_])',
    re.IGNORECASE,
)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    storage_uri TEXT NOT NULL UNIQUE,
    description TEXT,
    domain TEXT,
    source_type TEXT,
    content_type TEXT,
    size_bytes INTEGER,
    indexed_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS artifacts_fts USING fts5(
    name, description, domain,
    content='artifacts', content_rowid='rowid'
);

-- Triggers to keep FTS in sync with artifacts table
CREATE TRIGGER IF NOT EXISTS artifacts_ai AFTER INSERT ON artifacts BEGIN
    INSERT INTO artifacts_fts(rowid, name, description, domain)
    VALUES (new.rowid, new.name, new.description, new.domain);
END;

CREATE TRIGGER IF NOT EXISTS artifacts_ad AFTER DELETE ON artifacts BEGIN
    INSERT INTO artifacts_fts(artifacts_fts, rowid, name, description, domain)
    VALUES ('delete', old.rowid, old.name, old.description, old.domain);
END;

CREATE TRIGGER IF NOT EXISTS artifacts_au AFTER UPDATE ON artifacts BEGIN
    INSERT INTO artifacts_fts(artifacts_fts, rowid, name, description, domain)
    VALUES ('delete', old.rowid, old.name, old.description, old.domain);
    INSERT INTO artifacts_fts(rowid, name, description, domain)
    VALUES (new.rowid, new.name, new.description, new.domain);
END;
"""


@dataclass
class ArtifactRecord:
    """Represents a single artifact in the catalog."""

    id: str
    name: str
    storage_uri: str
    description: Optional[str] = None
    domain: Optional[str] = None
    source_type: Optional[str] = None
    content_type: Optional[str] = None
    size_bytes: Optional[int] = None
    indexed_at: Optional[str] = None
    score: Optional[float] = None

    def to_dict(self) -> dict:
        """Convert to dictionary, excluding None values and internal fields."""
        result = {}
        for field in (
            "id",
            "name",
            "storage_uri",
            "description",
            "domain",
            "source_type",
            "content_type",
            "size_bytes",
            "indexed_at",
        ):
            value = getattr(self, field)
            if value is not None:
                result[field] = value
        if self.score is not None:
            result["score"] = self.score
        return result


def artifact_id_from_uri(uri: str) -> str:
    """Generate a stable artifact ID from a storage URI."""
    return hashlib.sha256(uri.encode()).hexdigest()[:16]


def _serialize_vector(vec: list[float]) -> bytes:
    """Serialize a float vector to bytes for sqlite-vec."""
    return struct.pack(f"{len(vec)}f", *vec)


def _references_vector_table(sql: str) -> bool:
    """Return whether SQL references the vector table outside literals/comments."""
    searchable_sql = _SQL_IGNORED_RE.sub(" ", sql)
    return _VECTOR_TABLE_REFERENCE_RE.search(searchable_sql) is not None


class CatalogDB:
    """SQLite-backed catalog with mandatory FTS5 and on-demand vector search."""

    def __init__(self, db_path: str | Path = ":memory:", vec_dimensions: int | None = 768):
        if vec_dimensions is not None and vec_dimensions <= 0:
            raise ValueError("vec_dimensions must be greater than zero")
        self._db_path = str(db_path)
        self._vec_dimensions = vec_dimensions
        self._conn: Optional[sqlite3.Connection] = None
        self._vector_loaded = False

    def open(self) -> None:
        """Open the database connection and initialize the keyword-search schema."""
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        try:
            self._probe_fts5()
            self._conn.executescript(_SCHEMA_SQL)
            self._conn.commit()
        except Exception:
            self._conn.close()
            self._conn = None
            raise

    def close(self) -> None:
        """Close the database connection."""
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

    def execute_readonly(self, sql: str, max_rows: int = 100) -> list[dict]:
        """Execute a read-only SQL query and return results as dicts.

        Uses a separate read-only connection to prevent any writes.

        Args:
            sql: SQL query to execute (SELECT only).
            max_rows: Maximum number of rows to return.

        Returns:
            List of row dictionaries.

        Raises:
            sqlite3.OperationalError: If the query attempts a write operation.
            ValueError: If the query appears to be a write operation.
        """
        # Belt-and-suspenders: reject obvious write statements before execution
        stripped = sql.strip().upper()
        write_keywords = ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "REPLACE")
        if any(stripped.startswith(kw) for kw in write_keywords):
            raise ValueError(f"Write operations are not permitted. Query starts with: {stripped.split()[0]}")

        # Open a separate read-only connection
        read_conn = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
        read_conn.row_factory = sqlite3.Row

        try:
            if _references_vector_table(sql):
                self._ensure_vector_capability(read_conn, create_table=False)
            cursor = read_conn.execute(sql)
            rows = cursor.fetchmany(max_rows)
            return [{k: row[k] for k in row.keys()} for row in rows]
        finally:
            read_conn.close()

    def upsert_artifact(
        self,
        artifact_id: str,
        name: str,
        storage_uri: str,
        description: Optional[str] = None,
        domain: Optional[str] = None,
        source_type: Optional[str] = None,
        content_type: Optional[str] = None,
        size_bytes: Optional[int] = None,
        indexed_at: Optional[str] = None,
        embedding: Optional[list[float]] = None,
    ) -> None:
        """Insert or update an artifact record and its embedding."""
        if embedding is not None:
            self._validate_vector_dimensions(embedding, "artifact embedding")
            self._ensure_vector_capability(self.conn)

        self.conn.execute(
            """INSERT OR REPLACE INTO artifacts
               (id, name, storage_uri, description, domain, source_type, content_type, size_bytes, indexed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (artifact_id, name, storage_uri, description, domain, source_type, content_type, size_bytes, indexed_at),
        )
        if embedding is not None:
            # Delete existing vec entry if present, then insert
            self.conn.execute("DELETE FROM artifacts_vec WHERE id = ?", (artifact_id,))
            self.conn.execute(
                "INSERT INTO artifacts_vec (id, embedding) VALUES (?, ?)",
                (artifact_id, _serialize_vector(embedding)),
            )
        self.conn.commit()

    def upsert_artifacts_batch(self, artifacts: list[dict]) -> None:
        """Batch upsert multiple artifacts for efficiency."""
        for artifact in artifacts:
            self.upsert_artifact(**artifact)

    def get_artifact(self, artifact_id: str) -> Optional[ArtifactRecord]:
        """Retrieve a single artifact by ID."""
        row = self.conn.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
        if not row:
            return None
        return ArtifactRecord(**{k: row[k] for k in row.keys()})

    def get_existing_uris(self) -> set[str]:
        """Get all storage URIs currently in the database."""
        rows = self.conn.execute("SELECT storage_uri FROM artifacts").fetchall()
        return {row[0] for row in rows}

    def delete_artifacts(self, artifact_ids: list[str]) -> None:
        """Remove artifacts by ID."""
        if not artifact_ids:
            return
        placeholders = ",".join("?" * len(artifact_ids))
        has_vector_table = self._vector_loaded or self._has_vector_table(self.conn)
        if has_vector_table:
            self._ensure_vector_capability(self.conn)
        self.conn.execute(f"DELETE FROM artifacts WHERE id IN ({placeholders})", artifact_ids)
        if has_vector_table:
            self.conn.execute(f"DELETE FROM artifacts_vec WHERE id IN ({placeholders})", artifact_ids)
        self.conn.commit()

    def list_domains(self) -> list[str]:
        """Get all unique domains in the catalog."""
        rows = self.conn.execute(
            "SELECT DISTINCT domain FROM artifacts WHERE domain IS NOT NULL ORDER BY domain"
        ).fetchall()
        return [row[0] for row in rows]

    def search(
        self,
        query: str,
        query_embedding: Optional[list[float]] = None,
        domain: Optional[str] = None,
        source_type: Optional[str] = None,
        top: int = 10,
        hybrid_alpha: float = 0.5,
    ) -> list[ArtifactRecord]:
        """
        Hybrid search combining FTS5 keyword matching and vector similarity.

        Args:
            query: Search query text.
            query_embedding: Pre-computed embedding for the query (if available).
            domain: Filter to specific domain.
            source_type: Filter to 'local' or 'blob'.
            top: Maximum number of results.
            hybrid_alpha: Weight for FTS score (1 - alpha for vector score).
        """
        fts_scores: dict[str, float] = {}
        vec_scores: dict[str, float] = {}

        # FTS5 keyword search
        if query.strip():
            fts_rows = self.conn.execute(
                """SELECT a.id, rank
                   FROM artifacts_fts fts
                   JOIN artifacts a ON a.rowid = fts.rowid
                   WHERE artifacts_fts MATCH ?
                   ORDER BY rank
                   LIMIT ?""",
                (query, top * 3),
            ).fetchall()
            if fts_rows:
                # rank is negative (lower = better), normalize to 0-1
                min_rank = min(row[1] for row in fts_rows)
                max_rank = max(row[1] for row in fts_rows)
                range_rank = max_rank - min_rank if max_rank != min_rank else 1.0
                for row in fts_rows:
                    # Invert so higher = better, normalize to 0-1
                    fts_scores[row[0]] = 1.0 - (row[1] - min_rank) / range_rank

        # Vector search
        if query_embedding is not None:
            self._validate_vector_dimensions(query_embedding, "query embedding")
            self._ensure_vector_capability(self.conn)
            vec_rows = self.conn.execute(
                """SELECT id, distance
                   FROM artifacts_vec
                   WHERE embedding MATCH ?
                   ORDER BY distance
                   LIMIT ?""",
                (_serialize_vector(query_embedding), top * 3),
            ).fetchall()
            if vec_rows:
                # distance is cosine distance (lower = more similar)
                for row in vec_rows:
                    vec_scores[row[0]] = 1.0 - row[1]  # Convert to similarity

        # Combine scores
        all_ids = set(fts_scores.keys()) | set(vec_scores.keys())
        if not all_ids:
            # Fallback: return all artifacts matching filters
            return self._filter_all(domain, source_type, top)

        scored: list[tuple[str, float]] = []
        for aid in all_ids:
            fts_s = fts_scores.get(aid, 0.0)
            vec_s = vec_scores.get(aid, 0.0)
            combined = hybrid_alpha * fts_s + (1.0 - hybrid_alpha) * vec_s
            scored.append((aid, combined))

        scored.sort(key=lambda x: x[1], reverse=True)

        # Fetch full records and apply filters
        results: list[ArtifactRecord] = []
        for aid, score in scored:
            if len(results) >= top:
                break
            record = self.get_artifact(aid)
            if record is None:
                continue
            if domain and record.domain != domain:
                continue
            if source_type and record.source_type != source_type:
                continue
            record.score = score
            results.append(record)

        return results

    def _filter_all(self, domain: Optional[str], source_type: Optional[str], top: int) -> list[ArtifactRecord]:
        """Return all artifacts matching filters when search yields no results."""
        conditions = []
        params: list = []
        if domain:
            conditions.append("domain = ?")
            params.append(domain)
        if source_type:
            conditions.append("source_type = ?")
            params.append(source_type)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = self.conn.execute(
            f"SELECT * FROM artifacts {where} ORDER BY name LIMIT ?",
            params + [top],
        ).fetchall()
        return [ArtifactRecord(**{k: row[k] for k in row.keys()}) for row in rows]

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
    ) -> None:
        """Load sqlite-vec and validate/create the vector table on demand."""
        if conn is self._conn and self._vector_loaded:
            return

        had_active_transaction = conn.in_transaction
        try:
            sqlite_vec = import_module("sqlite_vec")
        except ImportError as exc:
            raise RuntimeError(
                "Catalog vector search requires sqlite-vec. "
                f"Install the '{_VECTOR_EXTRA}' extra and reopen the catalog."
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
