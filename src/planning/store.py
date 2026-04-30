"""
PlanStore — SQLite-backed plan persistence layer.

This module is framework-agnostic: it has no dependency on agent_framework.
All mutations are immediately written to the database and recorded in the
append-only history table.

Usage
-----
    # In-memory (ephemeral, backwards-compatible):
    store = PlanStore()

    # File-backed (survives process restarts):
    store = PlanStore("/tmp/my_plan.db")

    # Resume an existing plan:
    store = PlanStore.load("/tmp/my_plan.db", plan_id="<uuid>")

    # Multiple named plans in one DB:
    store_a = PlanStore("/tmp/shared.db", title="Research")
    store_b = PlanStore("/tmp/shared.db", title="Review")
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Generator, Optional

from .models import HistoryRecord, StepRecord, StepStatus


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


# Columns that may be updated via update_step(); used as an allowlist to
# prevent SQL column-name injection if the dict keys ever come from external input.
_ALLOWED_UPDATE_COLUMNS = frozenset({"description", "notes"})


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '',
    finalized   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS steps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id     TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    notes       TEXT NOT NULL DEFAULT '',
    order_index INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS step_deps (
    step_id     INTEGER NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
    depends_on  INTEGER NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
    PRIMARY KEY (step_id, depends_on)
);

CREATE TABLE IF NOT EXISTS step_tags (
    step_id     INTEGER NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
    tag         TEXT NOT NULL,
    PRIMARY KEY (step_id, tag)
);

CREATE TABLE IF NOT EXISTS step_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id     TEXT NOT NULL,
    step_id     INTEGER,
    action      TEXT NOT NULL,
    data        TEXT NOT NULL DEFAULT '{}',
    timestamp   TEXT NOT NULL
);
"""

_STATUS_ICONS = {
    StepStatus.PENDING: "[ ]",
    StepStatus.IN_PROGRESS: "[→]",
    StepStatus.COMPLETED: "[✓]",
    StepStatus.FAILED: "[✗]",
    StepStatus.SKIPPED: "[-]",
}


class PlanStore:
    """
    SQLite-backed plan data store.

    A ``PlanStore`` represents a single named plan inside a SQLite database.
    Multiple ``PlanStore`` instances may share the same database file (each
    with a different plan_id).

    Parameters
    ----------
    db_path:
        File path for the SQLite database.  Use ``":memory:"`` (the default)
        for an in-process ephemeral store.
    title:
        Human-readable plan title.  Used when creating a *new* plan.
    plan_id:
        If provided, the store opens an existing plan rather than creating a
        new one.  Raise ``ValueError`` if the plan does not exist.
    wal_mode:
        Enable SQLite WAL journal mode for better concurrency (default: True).
    """

    def __init__(
        self,
        db_path: str = ":memory:",
        title: str = "",
        plan_id: Optional[str] = None,
        wal_mode: bool = True,
    ) -> None:
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA foreign_keys = ON")
        if wal_mode and db_path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

        if plan_id is not None:
            row = self._conn.execute("SELECT id, title, finalized FROM plans WHERE id = ?", (plan_id,)).fetchone()
            if row is None:
                raise ValueError(f"Plan '{plan_id}' not found in database '{db_path}'")
            self._plan_id: str = row[0]
        else:
            self._plan_id = str(uuid.uuid4())
            self._conn.execute(
                "INSERT INTO plans (id, title, finalized, created_at) VALUES (?, ?, 0, ?)",
                (self._plan_id, title, _utcnow().isoformat()),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Class-level helpers
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, db_path: str, plan_id: str, wal_mode: bool = True) -> "PlanStore":
        """Load an existing plan from a database by its UUID."""
        return cls(db_path=db_path, plan_id=plan_id, wal_mode=wal_mode)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @contextmanager
    def _tx(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager that commits or rolls back a transaction."""
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def _record_history(
        self,
        action: str,
        data: dict,
        step_id: Optional[int] = None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO step_history (plan_id, step_id, action, data, timestamp) VALUES (?, ?, ?, ?, ?)",
            (self._plan_id, step_id, action, json.dumps(data), _utcnow().isoformat()),
        )

    def _fetch_step(self, step_id: int) -> StepRecord:
        row = self._conn.execute(
            "SELECT id, plan_id, description, status, notes, order_index, created_at, updated_at "
            "FROM steps WHERE id = ? AND plan_id = ?",
            (step_id, self._plan_id),
        ).fetchone()
        if row is None:
            raise ValueError(f"Step with id {step_id} not found")
        return self._row_to_record(row)

    def _row_to_record(self, row: tuple) -> StepRecord:
        sid = row[0]
        tags = tuple(
            r[0]
            for r in self._conn.execute("SELECT tag FROM step_tags WHERE step_id = ? ORDER BY tag", (sid,)).fetchall()
        )
        deps = tuple(
            r[0]
            for r in self._conn.execute(
                "SELECT depends_on FROM step_deps WHERE step_id = ? ORDER BY depends_on", (sid,)
            ).fetchall()
        )
        return StepRecord(
            step_id=row[0],
            plan_id=row[1],
            description=row[2],
            status=StepStatus(row[3]),
            notes=row[4],
            order_index=row[5],
            tags=tags,
            depends_on=deps,
            created_at=_parse_dt(row[6]),
            updated_at=_parse_dt(row[7]),
        )

    def _next_order_index(self) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(order_index), -1) + 1 FROM steps WHERE plan_id = ?",
            (self._plan_id,),
        ).fetchone()
        return row[0]

    # ------------------------------------------------------------------
    # Plan-level properties
    # ------------------------------------------------------------------

    @property
    def plan_id(self) -> str:
        return self._plan_id

    @property
    def title(self) -> str:
        row = self._conn.execute("SELECT title FROM plans WHERE id = ?", (self._plan_id,)).fetchone()
        return row[0] if row else ""

    @property
    def finalized(self) -> bool:
        row = self._conn.execute("SELECT finalized FROM plans WHERE id = ?", (self._plan_id,)).fetchone()
        return bool(row[0]) if row else False

    @finalized.setter
    def finalized(self, value: bool) -> None:
        with self._tx():
            self._conn.execute(
                "UPDATE plans SET finalized = ? WHERE id = ?",
                (1 if value else 0, self._plan_id),
            )

    @property
    def steps(self) -> list[StepRecord]:
        rows = self._conn.execute(
            "SELECT id, plan_id, description, status, notes, order_index, created_at, updated_at "
            "FROM steps WHERE plan_id = ? ORDER BY order_index",
            (self._plan_id,),
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    # ------------------------------------------------------------------
    # Core CRUD (matches existing Plan interface)
    # ------------------------------------------------------------------

    def add_step(self, description: str) -> StepRecord:
        """Add a new step to the end of the plan."""
        now = _utcnow().isoformat()
        with self._tx():
            order_index = self._next_order_index()
            cursor = self._conn.execute(
                "INSERT INTO steps (plan_id, description, status, notes, order_index, created_at, updated_at) "
                "VALUES (?, ?, 'pending', '', ?, ?, ?)",
                (self._plan_id, description, order_index, now, now),
            )
            step_id = cursor.lastrowid
            assert step_id is not None
            self._record_history("add_step", {"description": description}, step_id)
        return self._fetch_step(step_id)

    def insert_step(self, after_step_id: int, description: str) -> StepRecord:
        """Insert a new step after the given step_id.

        Use ``after_step_id=0`` to insert at the beginning.

        Raises:
            ValueError: If after_step_id is not found (and is not 0).
        """
        now = _utcnow().isoformat()
        with self._tx():
            if after_step_id == 0:
                insert_order = -0.5
            else:
                row = self._conn.execute(
                    "SELECT order_index FROM steps WHERE id = ? AND plan_id = ?",
                    (after_step_id, self._plan_id),
                ).fetchone()
                if row is None:
                    raise ValueError(f"Step with id {after_step_id} not found")
                insert_order = row[0] + 0.5

            cursor = self._conn.execute(
                "INSERT INTO steps (plan_id, description, status, notes, order_index, created_at, updated_at) "
                "VALUES (?, ?, 'pending', '', ?, ?, ?)",
                (self._plan_id, description, insert_order, now, now),
            )
            step_id = cursor.lastrowid
            assert step_id is not None

            # Renumber all steps to clean integer order_index values
            rows = self._conn.execute(
                "SELECT id FROM steps WHERE plan_id = ? ORDER BY order_index",
                (self._plan_id,),
            ).fetchall()
            for i, (sid,) in enumerate(rows):
                self._conn.execute("UPDATE steps SET order_index = ? WHERE id = ?", (i, sid))

            self._record_history(
                "insert_step",
                {"after_step_id": after_step_id, "description": description},
                step_id,
            )
        return self._fetch_step(step_id)

    def update_step(
        self,
        step_id: int,
        description: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> StepRecord:
        """Update a step's description and/or notes.

        Raises:
            ValueError: If step_id is not found.
        """
        step = self._fetch_step(step_id)
        now = _utcnow().isoformat()
        changes: dict = {}
        if description is not None:
            changes["description"] = description
        if notes is not None:
            changes["notes"] = notes
        if not changes:
            return step
        # Guard against SQL column-name injection: only allow known safe columns.
        for k in changes:
            if k not in _ALLOWED_UPDATE_COLUMNS:
                raise ValueError(f"Column '{k}' is not allowed in update_step.")
        with self._tx():
            sets = ", ".join(f"{k} = ?" for k in changes)
            vals = list(changes.values()) + [now, step_id, self._plan_id]
            self._conn.execute(
                f"UPDATE steps SET {sets}, updated_at = ? WHERE id = ? AND plan_id = ?",
                vals,
            )
            self._record_history("update_step", changes, step_id)
        return self._fetch_step(step_id)

    def set_step_status(
        self,
        step_id: int,
        status: StepStatus,
        notes: Optional[str] = None,
    ) -> StepRecord:
        """Set the status (and optionally notes) of a step.

        Raises:
            ValueError: If step_id is not found.
        """
        self._fetch_step(step_id)  # validate existence
        now = _utcnow().isoformat()
        with self._tx():
            if notes is not None:
                self._conn.execute(
                    "UPDATE steps SET status = ?, notes = ?, updated_at = ? WHERE id = ? AND plan_id = ?",
                    (status.value, notes, now, step_id, self._plan_id),
                )
            else:
                self._conn.execute(
                    "UPDATE steps SET status = ?, updated_at = ? WHERE id = ? AND plan_id = ?",
                    (status.value, now, step_id, self._plan_id),
                )
            self._record_history(
                "set_step_status",
                {"status": status.value, "notes": notes},
                step_id,
            )
        return self._fetch_step(step_id)

    def remove_step(self, step_id: int) -> StepRecord:
        """Remove a step from the plan.

        Raises:
            ValueError: If step_id is not found.
        """
        step = self._fetch_step(step_id)
        with self._tx():
            self._record_history("remove_step", {"description": step.description}, step_id)
            self._conn.execute("DELETE FROM steps WHERE id = ? AND plan_id = ?", (step_id, self._plan_id))
        return step

    # ------------------------------------------------------------------
    # Dependency management
    # ------------------------------------------------------------------

    def add_dependency(self, step_id: int, depends_on: int) -> None:
        """Add a dependency edge: *step_id* is blocked until *depends_on* completes.

        Raises:
            ValueError: If either step is not found, or if adding the edge
                would create a cycle.
        """
        self._fetch_step(step_id)
        self._fetch_step(depends_on)
        if step_id == depends_on:
            raise ValueError("A step cannot depend on itself")
        if self._would_create_cycle(step_id, depends_on):
            raise ValueError(f"Adding dependency {step_id} → {depends_on} would create a cycle")
        with self._tx():
            self._conn.execute(
                "INSERT OR IGNORE INTO step_deps (step_id, depends_on) VALUES (?, ?)",
                (step_id, depends_on),
            )
            self._record_history(
                "add_dependency",
                {"step_id": step_id, "depends_on": depends_on},
                step_id=step_id,
            )

    def remove_dependency(self, step_id: int, depends_on: int) -> None:
        """Remove a dependency edge between two steps.

        Raises:
            ValueError: If either step is not found.
        """
        self._fetch_step(step_id)
        self._fetch_step(depends_on)
        with self._tx():
            self._conn.execute(
                "DELETE FROM step_deps WHERE step_id = ? AND depends_on = ?",
                (step_id, depends_on),
            )
            self._record_history(
                "remove_dependency",
                {"step_id": step_id, "depends_on": depends_on},
                step_id=step_id,
            )

    def _would_create_cycle(self, new_step_id: int, new_depends_on: int) -> bool:
        """Return True if adding new_step_id → new_depends_on creates a cycle."""
        # BFS/DFS from new_depends_on; if we reach new_step_id, it's a cycle
        visited: set[int] = set()
        queue = [new_depends_on]
        while queue:
            current = queue.pop()
            if current == new_step_id:
                return True
            if current in visited:
                continue
            visited.add(current)
            rows = self._conn.execute("SELECT depends_on FROM step_deps WHERE step_id = ?", (current,)).fetchall()
            queue.extend(r[0] for r in rows)
        return False

    # ------------------------------------------------------------------
    # Tag management
    # ------------------------------------------------------------------

    def tag_step(self, step_id: int, tag: str) -> None:
        """Attach a label to a step.

        Raises:
            ValueError: If step_id is not found.
        """
        self._fetch_step(step_id)
        with self._tx():
            self._conn.execute(
                "INSERT OR IGNORE INTO step_tags (step_id, tag) VALUES (?, ?)",
                (step_id, tag),
            )
            self._record_history("tag_step", {"tag": tag}, step_id)

    def untag_step(self, step_id: int, tag: str) -> None:
        """Remove a label from a step.

        Raises:
            ValueError: If step_id is not found.
        """
        self._fetch_step(step_id)
        with self._tx():
            self._conn.execute("DELETE FROM step_tags WHERE step_id = ? AND tag = ?", (step_id, tag))
            self._record_history("untag_step", {"tag": tag}, step_id)

    # ------------------------------------------------------------------
    # Querying
    # ------------------------------------------------------------------

    def query_steps(
        self,
        status: Optional[StepStatus] = None,
        tag: Optional[str] = None,
        ready_only: bool = False,
    ) -> list[StepRecord]:
        """Filter steps by status, tag, and/or dependency readiness.

        Parameters
        ----------
        status:
            If provided, only steps with this status are returned.
        tag:
            If provided, only steps with this tag are returned.
        ready_only:
            If True, only steps whose dependencies are all completed are returned.
        """
        query = (
            "SELECT DISTINCT s.id, s.plan_id, s.description, s.status, s.notes, "
            "s.order_index, s.created_at, s.updated_at "
            "FROM steps s "
        )
        conditions: list[str] = ["s.plan_id = ?"]
        params: list = [self._plan_id]

        if tag is not None:
            query += "JOIN step_tags t ON t.step_id = s.id "
            conditions.append("t.tag = ?")
            params.append(tag)

        if ready_only:
            query += "LEFT JOIN step_deps d ON d.step_id = s.id LEFT JOIN steps dep ON dep.id = d.depends_on "

        if status is not None:
            conditions.append("s.status = ?")
            params.append(status.value)

        query += "WHERE " + " AND ".join(conditions)

        if ready_only:
            query += (
                " GROUP BY s.id, s.plan_id, s.description, s.status, "
                "s.notes, s.order_index, s.created_at, s.updated_at "
                "HAVING SUM(CASE "
                "WHEN dep.status IS NOT NULL AND dep.status != ? "
                "THEN 1 ELSE 0 END) = 0"
            )
            params.append(StepStatus.COMPLETED.value)

        query += " ORDER BY s.order_index"

        rows = self._conn.execute(query, params).fetchall()
        return [self._row_to_record(r) for r in rows]

    def _is_ready(self, step_id: int) -> bool:
        """Return True if all dependencies of *step_id* are completed."""
        deps = self._conn.execute(
            "SELECT d.depends_on, s.status FROM step_deps d JOIN steps s ON s.id = d.depends_on WHERE d.step_id = ?",
            (step_id,),
        ).fetchall()
        return all(StepStatus(r[1]) == StepStatus.COMPLETED for r in deps)

    def ready_steps(self) -> list[StepRecord]:
        """Return steps that are pending and whose dependencies are all completed."""
        return self.query_steps(status=StepStatus.PENDING, ready_only=True)

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    def get_history(self, step_id: Optional[int] = None) -> list[HistoryRecord]:
        """Retrieve the change log for a step or the whole plan.

        Parameters
        ----------
        step_id:
            If provided, return only entries for that step.  Otherwise return
            all history for the plan.
        """
        if step_id is not None:
            rows = self._conn.execute(
                "SELECT id, plan_id, step_id, action, data, timestamp "
                "FROM step_history WHERE plan_id = ? AND step_id = ? ORDER BY id",
                (self._plan_id, step_id),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT id, plan_id, step_id, action, data, timestamp FROM step_history WHERE plan_id = ? ORDER BY id",
                (self._plan_id,),
            ).fetchall()
        return [
            HistoryRecord(
                history_id=r[0],
                plan_id=r[1],
                step_id=r[2],
                action=r[3],
                data=r[4],
                timestamp=_parse_dt(r[5]) or _utcnow(),
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Plan lifecycle (matches existing Plan interface)
    # ------------------------------------------------------------------

    def finalize(self) -> str:
        """Mark the plan as finalized (ready for execution)."""
        if not self.steps:
            return "Cannot finalize an empty plan."
        self.finalized = True
        with self._tx():
            self._record_history("finalize", {})
        count = len(self.steps)
        return f"Plan finalized with {count} steps. Proceeding to execution phase."

    def is_complete(self) -> bool:
        """Check whether all steps have a terminal status."""
        steps = self.steps
        if not steps:
            return False
        terminal = {StepStatus.COMPLETED, StepStatus.FAILED, StepStatus.SKIPPED}
        return all(s.status in terminal for s in steps)

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    def view(self) -> str:
        """Return a human-readable ASCII view of the plan for LLM consumption."""
        steps = self.steps
        if not steps:
            return "Plan is empty. No steps have been added yet."

        title_part = f" — {self.title}" if self.title else ""
        lines = [f"Plan{title_part} ({len(steps)} steps, finalized={self.finalized}):"]
        for step in steps:
            icon = _STATUS_ICONS[step.status]
            line = f"  {step.step_id}. {icon} {step.description}"
            if step.tags:
                line += f"  [{', '.join(step.tags)}]"
            if step.notes:
                line += f"  — {step.notes}"
            lines.append(line)
        return "\n".join(lines)

    def summary(self) -> dict:
        """Return counts of steps by status."""
        steps = self.steps
        counts: dict[str, int] = {s.value: 0 for s in StepStatus}
        for step in steps:
            counts[step.status.value] += 1
        counts["total"] = len(steps)
        return counts

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """Serialize the plan (metadata + all steps) to a plain dictionary."""
        return {
            "plan_id": self._plan_id,
            "title": self.title,
            "finalized": self.finalized,
            "steps": [s.to_dict() for s in self.steps],
            "summary": self.summary(),
        }

    def to_json(self) -> str:
        """Serialize the plan to a JSON string."""
        import json as _json

        return _json.dumps(self.to_dict(), default=str)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

    def __enter__(self) -> "PlanStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
