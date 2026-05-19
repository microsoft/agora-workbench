"""SQLite catalog database with FTS5 and sqlite-vec for hybrid search."""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import sqlite_vec

LOGGER = logging.getLogger(__name__)

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


class CatalogDB:
    """SQLite-backed catalog with FTS5 keyword search and sqlite-vec vector search."""

    def __init__(self, db_path: str | Path = ":memory:", vec_dimensions: int = 768):
        self._db_path = str(db_path)
        self._vec_dimensions = vec_dimensions
        self._conn: Optional[sqlite3.Connection] = None

    def open(self) -> None:
        """Open the database connection and initialize schema."""
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.enable_load_extension(True)
        sqlite_vec.load(self._conn)
        self._conn.enable_load_extension(False)
        self._conn.executescript(_SCHEMA_SQL)
        # Create vec table for vector search
        self._conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS artifacts_vec USING vec0("
            f"  id TEXT PRIMARY KEY, embedding float[{self._vec_dimensions}]"
            f")"
        )
        self._conn.commit()

    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Database not opened. Call open() first.")
        return self._conn

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
        self.conn.execute(
            """INSERT OR REPLACE INTO artifacts
               (id, name, storage_uri, description, domain, source_type, content_type, size_bytes, indexed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (artifact_id, name, storage_uri, description, domain, source_type, content_type, size_bytes, indexed_at),
        )
        if embedding:
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
        self.conn.execute(f"DELETE FROM artifacts WHERE id IN ({placeholders})", artifact_ids)
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
        if query_embedding:
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
