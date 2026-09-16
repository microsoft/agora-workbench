"""SQLite-backed object storage and catalog integration for the pandas example."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit

from agora_workbench.data_lake import (
    READ_OPERATIONS,
    ArtifactNotFoundError,
    ArtifactPresentation,
    ArtifactReference,
    CatalogArtifact,
    InvalidRequestError,
    ListRequest,
    Page,
    RequestContext,
    ResolvedArtifact,
    SearchRequest,
    SourceCapabilities,
    StorageLocator,
)
from agora_workbench.data_lake.execution import AssetFetcher
from agora_workbench.data_lake.transfer import (
    TransferOptions,
    TransferResult,
    check_transfer_size,
    stream_chunks_to_file,
)

SQLITE_LAKE_SCHEME = "sqlite-lake"


@dataclass(frozen=True)
class SQLiteArtifactRecord:
    """One retained artifact revision stored in SQLite."""

    source_id: str
    artifact_id: str
    revision: int
    name: str
    description: str | None
    media_type: str | None
    size_bytes: int
    checksum_sha256: str
    metadata: Mapping[str, object]


class SQLiteObjectLake:
    """Store artifact metadata, revisions, and bytes in one local SQLite file."""

    def __init__(self, database_path: str | Path, *, source_id: str = "pandas-data") -> None:
        self.database_path = Path(database_path).expanduser().resolve()
        self.source_id = source_id

    def initialize(self) -> None:
        """Create the local database schema if it does not exist."""
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS artifact_revisions (
                    source_id TEXT NOT NULL,
                    artifact_id TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK (revision >= 1),
                    name TEXT NOT NULL,
                    description TEXT,
                    media_type TEXT,
                    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
                    checksum_sha256 TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    content BLOB NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (source_id, artifact_id, revision)
                );

                CREATE TABLE IF NOT EXISTS artifacts (
                    source_id TEXT NOT NULL,
                    artifact_id TEXT NOT NULL,
                    current_revision INTEGER NOT NULL CHECK (current_revision >= 1),
                    PRIMARY KEY (source_id, artifact_id),
                    FOREIGN KEY (source_id, artifact_id, current_revision)
                        REFERENCES artifact_revisions (source_id, artifact_id, revision)
                );

                CREATE INDEX IF NOT EXISTS idx_artifact_revisions_name
                    ON artifact_revisions (source_id, name);
                """
            )

    def put_object(
        self,
        artifact_id: str,
        content: bytes,
        *,
        name: str | None = None,
        description: str | None = None,
        media_type: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> ArtifactReference:
        """Insert a new immutable revision and make it the current revision."""
        if not artifact_id:
            raise InvalidRequestError("Artifact ID must be non-empty.", operation="upload")
        if not isinstance(content, bytes):
            raise TypeError("SQLite object content must be bytes.")

        normalized_metadata = dict(metadata or {})
        try:
            metadata_json = json.dumps(normalized_metadata, separators=(",", ":"), sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise InvalidRequestError("Artifact metadata must be JSON serializable.", operation="upload") from exc

        checksum = hashlib.sha256(content).hexdigest()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT current_revision
                FROM artifacts
                WHERE source_id = ? AND artifact_id = ?
                """,
                (self.source_id, artifact_id),
            ).fetchone()
            revision = 1 if row is None else int(row["current_revision"]) + 1
            connection.execute(
                """
                INSERT INTO artifact_revisions (
                    source_id, artifact_id, revision, name, description,
                    media_type, size_bytes, checksum_sha256, metadata_json,
                    content, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.source_id,
                    artifact_id,
                    revision,
                    name or artifact_id,
                    description,
                    media_type,
                    len(content),
                    checksum,
                    metadata_json,
                    content,
                    datetime.now(UTC).isoformat(),
                ),
            )
            connection.execute(
                """
                INSERT INTO artifacts (source_id, artifact_id, current_revision)
                VALUES (?, ?, ?)
                ON CONFLICT (source_id, artifact_id)
                DO UPDATE SET current_revision = excluded.current_revision
                """,
                (self.source_id, artifact_id, revision),
            )
        return ArtifactReference(artifact_id, self.source_id, revision)

    def count_objects(self) -> int:
        """Return the number of current logical artifacts."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS object_count FROM artifacts WHERE source_id = ?",
                (self.source_id,),
            ).fetchone()
        return int(row["object_count"])

    def get_record(self, reference: ArtifactReference) -> SQLiteArtifactRecord:
        """Return the exact requested revision, or the current revision when unpinned."""
        self._validate_source(reference.source_id)
        with self._connect() as connection:
            row = self._select_record(connection, reference)
        if row is None:
            raise ArtifactNotFoundError(
                f"Unknown artifact: {reference.artifact_id}",
                resource_id=reference.artifact_id,
                operation="get",
            )
        return self._record_from_row(row)

    def query_records(
        self,
        *,
        query: str | None,
        source_ids: tuple[str, ...],
        filters: Mapping[str, object],
        offset: int,
        limit: int,
    ) -> tuple[tuple[SQLiteArtifactRecord, ...], bool]:
        """Query current artifacts and report whether another page exists."""
        self._validate_filters(filters)
        if source_ids and self.source_id not in source_ids:
            return (), False

        clauses = ["a.source_id = ?"]
        parameters: list[object] = [self.source_id]
        normalized_query = (query or "").strip().lower()
        if normalized_query:
            clauses.append(
                """
                (
                    lower(r.artifact_id) LIKE ?
                    OR lower(r.name) LIKE ?
                    OR lower(COALESCE(r.description, '')) LIKE ?
                    OR lower(r.metadata_json) LIKE ?
                )
                """
            )
            pattern = f"%{normalized_query}%"
            parameters.extend((pattern, pattern, pattern, pattern))
        if filters.get("domain") is not None:
            clauses.append("json_extract(r.metadata_json, '$.domain') = ?")
            parameters.append(filters["domain"])
        if filters.get("source_type") is not None:
            clauses.append("COALESCE(json_extract(r.metadata_json, '$.source_type'), 'sqlite') = ?")
            parameters.append(filters["source_type"])

        parameters.extend((limit + 1, offset))
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT r.*
                FROM artifacts AS a
                JOIN artifact_revisions AS r
                  ON r.source_id = a.source_id
                 AND r.artifact_id = a.artifact_id
                 AND r.revision = a.current_revision
                WHERE {" AND ".join(clauses)}
                ORDER BY lower(r.name), r.artifact_id
                LIMIT ? OFFSET ?
                """,
                parameters,
            ).fetchall()
        return tuple(self._record_from_row(row) for row in rows[:limit]), len(rows) > limit

    def read_bytes(self, source_id: str, artifact_id: str, revision: int) -> bytes:
        """Read one exact object revision into memory."""
        self._validate_source(source_id)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT content
                FROM artifact_revisions
                WHERE source_id = ? AND artifact_id = ? AND revision = ?
                """,
                (source_id, artifact_id, revision),
            ).fetchone()
        if row is None:
            raise ArtifactNotFoundError(
                f"Unknown artifact revision: {artifact_id}@{revision}",
                resource_id=artifact_id,
                operation="download",
            )
        return bytes(row["content"])

    def open_blob(self, source_id: str, artifact_id: str, revision: int) -> tuple[sqlite3.Connection, sqlite3.Blob]:
        """Open an exact object revision for incremental BLOB reads."""
        self._validate_source(source_id)
        connection = self._connect(check_same_thread=False)
        try:
            row = connection.execute(
                """
                SELECT rowid
                FROM artifact_revisions
                WHERE source_id = ? AND artifact_id = ? AND revision = ?
                """,
                (source_id, artifact_id, revision),
            ).fetchone()
            if row is None:
                raise ArtifactNotFoundError(
                    f"Unknown artifact revision: {artifact_id}@{revision}",
                    resource_id=artifact_id,
                    operation="download",
                )
            return connection, connection.blobopen("artifact_revisions", "content", int(row["rowid"]), readonly=True)
        except BaseException:
            connection.close()
            raise

    def locator_for(self, reference: ArtifactReference) -> str:
        """Build a locator for a pinned artifact revision."""
        if reference.revision is None:
            raise InvalidRequestError("SQLite locators require a pinned revision.", operation="resolve")
        self._validate_source(reference.source_id)
        source = quote(reference.source_id, safe="")
        artifact = quote(reference.artifact_id, safe="")
        return f"{SQLITE_LAKE_SCHEME}://{source}/{artifact}?revision={reference.revision}"

    def _connect(self, *, check_same_thread: bool = True) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=30,
            check_same_thread=check_same_thread,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _select_record(
        self,
        connection: sqlite3.Connection,
        reference: ArtifactReference,
    ) -> sqlite3.Row | None:
        if reference.revision is None:
            return connection.execute(
                """
                SELECT r.*
                FROM artifacts AS a
                JOIN artifact_revisions AS r
                  ON r.source_id = a.source_id
                 AND r.artifact_id = a.artifact_id
                 AND r.revision = a.current_revision
                WHERE a.source_id = ? AND a.artifact_id = ?
                """,
                (reference.source_id, reference.artifact_id),
            ).fetchone()
        return connection.execute(
            """
            SELECT *
            FROM artifact_revisions
            WHERE source_id = ? AND artifact_id = ? AND revision = ?
            """,
            (reference.source_id, reference.artifact_id, reference.revision),
        ).fetchone()

    def _record_from_row(self, row: sqlite3.Row) -> SQLiteArtifactRecord:
        return SQLiteArtifactRecord(
            source_id=str(row["source_id"]),
            artifact_id=str(row["artifact_id"]),
            revision=int(row["revision"]),
            name=str(row["name"]),
            description=str(row["description"]) if row["description"] is not None else None,
            media_type=str(row["media_type"]) if row["media_type"] is not None else None,
            size_bytes=int(row["size_bytes"]),
            checksum_sha256=str(row["checksum_sha256"]),
            metadata=json.loads(str(row["metadata_json"])),
        )

    def _validate_source(self, source_id: str) -> None:
        if source_id != self.source_id:
            raise ArtifactNotFoundError(
                f"Unknown source: {source_id}",
                resource_id=source_id,
                operation="resolve",
            )

    @staticmethod
    def _validate_filters(filters: Mapping[str, object]) -> None:
        unsupported = set(filters) - {"domain", "source_type"}
        if unsupported:
            names = ", ".join(sorted(unsupported))
            raise InvalidRequestError(f"Unsupported SQLite catalog filters: {names}.", operation="search")


class SQLiteCatalogProvider:
    """Expose the SQLite object store through the public catalog contract."""

    def __init__(self, lake: SQLiteObjectLake, *, seed_demo_data: bool = False) -> None:
        self.lake = lake
        self.seed_demo_data = seed_demo_data

    async def load(self) -> None:
        if self.seed_demo_data:
            await asyncio.to_thread(seed_demo_objects, self.lake)
        else:
            await asyncio.to_thread(self.lake.initialize)

    async def aclose(self) -> None:
        return None

    async def capabilities(self) -> tuple[SourceCapabilities, ...]:
        return (SourceCapabilities(self.lake.source_id, READ_OPERATIONS),)

    async def search(self, request: SearchRequest, context: RequestContext) -> Page[CatalogArtifact]:
        del context
        return await self._query(
            query=request.query,
            source_ids=request.source_ids,
            filters=request.filters,
            limit=request.page.limit,
            cursor=request.page.cursor,
        )

    async def list(self, request: ListRequest, context: RequestContext) -> Page[CatalogArtifact]:
        del context
        return await self._query(
            query=None,
            source_ids=request.source_ids,
            filters=request.filters,
            limit=request.page.limit,
            cursor=request.page.cursor,
        )

    async def get(self, reference: ArtifactReference, context: RequestContext) -> CatalogArtifact:
        del context
        record = await asyncio.to_thread(self.lake.get_record, reference)
        return self._as_catalog_artifact(record, pin_reference=reference.revision is not None)

    async def resolve(self, reference: ArtifactReference, context: RequestContext) -> ResolvedArtifact:
        del context
        record = await asyncio.to_thread(self.lake.get_record, reference)
        pinned = ArtifactReference(record.artifact_id, record.source_id, record.revision)
        return ResolvedArtifact(pinned, StorageLocator(self.lake.locator_for(pinned)))

    async def _query(
        self,
        *,
        query: str | None,
        source_ids: tuple[str, ...],
        filters: Mapping[str, object],
        limit: int,
        cursor: str | None,
    ) -> Page[CatalogArtifact]:
        offset = _decode_cursor(cursor)
        records, has_more = await asyncio.to_thread(
            self.lake.query_records,
            query=query,
            source_ids=source_ids,
            filters=filters,
            offset=offset,
            limit=limit,
        )
        next_cursor = _encode_cursor(offset + limit) if has_more else None
        return Page(tuple(self._as_catalog_artifact(record) for record in records), next_cursor)

    def _as_catalog_artifact(
        self,
        record: SQLiteArtifactRecord,
        *,
        pin_reference: bool = False,
    ) -> CatalogArtifact:
        reference = ArtifactReference(
            record.artifact_id,
            record.source_id,
            record.revision if pin_reference else None,
        )
        pinned = ArtifactReference(record.artifact_id, record.source_id, record.revision)
        metadata = {
            **record.metadata,
            "source_type": record.metadata.get("source_type", "sqlite"),
        }
        return CatalogArtifact(
            reference=reference,
            presentation=ArtifactPresentation(
                name=record.name,
                description=record.description,
                media_type=record.media_type,
                size_bytes=record.size_bytes,
            ),
            locator=StorageLocator(self.lake.locator_for(pinned)),
            metadata=metadata,
            revision=record.revision,
            content_revision=record.checksum_sha256,
            metadata_revision=_metadata_revision(record),
            checksum_sha256=record.checksum_sha256,
        )


class SQLiteObjectFetcher(AssetFetcher):
    """Materialize SQLite BLOB locators into a code-execution session cache."""

    def __init__(self, lake: SQLiteObjectLake) -> None:
        super().__init__(credential=None)
        self.lake = lake

    def can_handle(self, qualified_name: str) -> bool:
        return qualified_name.startswith(f"{SQLITE_LAKE_SCHEME}://")

    async def fetch(self, qualified_name: str) -> bytes:
        source_id, artifact_id, revision = _parse_locator(qualified_name)
        return await asyncio.to_thread(self.lake.read_bytes, source_id, artifact_id, revision)

    async def fetch_to_file(
        self,
        qualified_name: str,
        dest_path: Any,
        *,
        options: TransferOptions | None = None,
        context: RequestContext | None = None,
    ) -> int:
        result = await self.fetch_to_file_result(
            qualified_name,
            dest_path,
            options=options,
            context=context,
        )
        return result.bytes_transferred

    async def fetch_to_file_result(
        self,
        qualified_name: str,
        dest_path: Any,
        *,
        options: TransferOptions | None = None,
        context: RequestContext | None = None,
    ) -> TransferResult:
        source_id, artifact_id, revision = _parse_locator(qualified_name)
        options = options or TransferOptions()
        context = context or RequestContext()
        connection, blob = await asyncio.to_thread(self.lake.open_blob, source_id, artifact_id, revision)
        try:
            check_transfer_size(
                len(blob),
                options,
                operation="download",
                resource=qualified_name,
            )

            async def chunks():
                while True:
                    chunk = await asyncio.to_thread(blob.read, options.chunk_size)
                    if not chunk:
                        break
                    yield chunk

            return await stream_chunks_to_file(
                chunks(),
                dest_path,
                options=options,
                context=context,
                resource=qualified_name,
            )
        finally:
            try:
                blob.close()
            finally:
                connection.close()


def seed_demo_objects(lake: SQLiteObjectLake) -> None:
    """Populate a new lake with small CSV objects that pandas can load."""
    lake.initialize()
    if lake.count_objects() > 0:
        return
    lake.put_object(
        "sales.csv",
        b"region,quarter,revenue\nwest,Q1,120\nwest,Q2,135\neast,Q1,98\neast,Q2,110\n",
        name="sales.csv",
        description="Synthetic quarterly revenue by region.",
        media_type="text/csv",
        metadata={"domain": "tabular", "source_type": "sqlite", "topic": "sales"},
    )
    lake.put_object(
        "customers.csv",
        b"customer_id,segment,active\n1,enterprise,true\n2,small-business,true\n3,consumer,false\n",
        name="customers.csv",
        description="Synthetic customer segments and active status.",
        media_type="text/csv",
        metadata={"domain": "tabular", "source_type": "sqlite", "topic": "customers"},
    )


def _parse_locator(locator: str) -> tuple[str, str, int]:
    parsed = urlsplit(locator)
    if parsed.scheme != SQLITE_LAKE_SCHEME or not parsed.netloc or not parsed.path.startswith("/"):
        raise InvalidRequestError("Malformed SQLite data-lake locator.", operation="download")
    query = parse_qs(parsed.query, strict_parsing=True)
    revisions = query.get("revision")
    if set(query) != {"revision"} or revisions is None or len(revisions) != 1:
        raise InvalidRequestError("SQLite data-lake locator requires one revision.", operation="download")
    try:
        revision = int(revisions[0])
    except ValueError as exc:
        raise InvalidRequestError("SQLite data-lake revision must be an integer.", operation="download") from exc
    if revision < 1:
        raise InvalidRequestError("SQLite data-lake revision must be at least 1.", operation="download")
    return unquote(parsed.netloc), unquote(parsed.path[1:]), revision


def _metadata_revision(record: SQLiteArtifactRecord) -> str:
    payload = json.dumps(
        {
            "name": record.name,
            "description": record.description,
            "media_type": record.media_type,
            "metadata": record.metadata,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode()).decode().rstrip("=")


def _decode_cursor(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        decoded = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        offset = int(decoded)
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise InvalidRequestError("Invalid SQLite catalog cursor.", operation="pagination") from exc
    if offset < 0:
        raise InvalidRequestError("Invalid SQLite catalog cursor.", operation="pagination")
    return offset
