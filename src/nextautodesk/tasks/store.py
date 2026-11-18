"""SQLite persistence for composed tasks.

The store is intentionally small and dependency-free (stdlib ``sqlite3``):

* one connection guarded by a process-wide re-entrant lock (the API runs a
  single process; SQLite is only an embedded durability layer),
* WAL journal mode so readers never block the serial worker,
* task steps / step results / error objects stored as JSON columns,
* timestamps stored as ISO-8601 UTC strings (lexicographically sortable),
* ``recover_interrupted()`` flips ``queued``/``running`` records to
  ``interrupted`` on startup so a container restart never leaves ghost tasks.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..config import AppConfig
from ..errors import StorageError
from ..models import TaskRecord, TaskStatus, utc_now

LOGGER = logging.getLogger(__name__)

#: Every task status the store knows about (used by ``counts``).
ALL_STATUSES: tuple[str, ...] = (
    "queued",
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "timed_out",
    "interrupted",
)

#: Columns of the ``tasks`` table.
COLUMNS: tuple[str, ...] = (
    "id",
    "name",
    "status",
    "created_at",
    "queued_at",
    "started_at",
    "finished_at",
    "timeout_ms",
    "cancel_requested_at",
    "error_json",
    "steps_json",
    "step_results_json",
    "metadata_json",
    "client_request_id",
)

#: Record attribute -> column mapping for JSON encoded values.
_JSON_FIELDS = {
    "error": "error_json",
    "steps": "steps_json",
    "step_results": "step_results_json",
    "metadata": "metadata_json",
}
_DATETIME_FIELDS = {"created_at", "queued_at", "started_at", "finished_at", "cancel_requested_at"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id                  TEXT PRIMARY KEY,
    name                TEXT,
    status              TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    queued_at           TEXT NOT NULL,
    started_at          TEXT,
    finished_at         TEXT,
    timeout_ms          INTEGER,
    cancel_requested_at TEXT,
    error_json          TEXT,
    steps_json          TEXT NOT NULL DEFAULT '[]',
    step_results_json   TEXT NOT NULL DEFAULT '[]',
    metadata_json       TEXT NOT NULL DEFAULT '{}',
    client_request_id   TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at);
"""


def _utc_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _json_loads(value: str | None, default: Any) -> Any:
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):  # pragma: no cover - corrupted rows are logged
        LOGGER.warning("ignoring corrupt JSON column in task record")
        return default


def _model_json(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


class TaskStore:
    """Thread-safe SQLite task store."""

    def __init__(
        self,
        config: AppConfig,
        *,
        database_path: str | Path | None = None,
    ) -> None:
        self.config = config
        self.database_path = Path(database_path) if database_path else config.storage.database_path
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    # -- lifecycle ---------------------------------------------------------
    def initialize(self) -> None:
        """Create directories, open the database and apply the schema."""

        with self._lock:
            if self._conn is not None:
                return
            try:
                self.database_path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise StorageError(
                    f"cannot create task database directory {self.database_path.parent}: {exc}"
                ) from exc
            try:
                connection = sqlite3.connect(
                    str(self.database_path),
                    check_same_thread=False,
                    timeout=30.0,
                )
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=NORMAL")
                connection.execute("PRAGMA busy_timeout=5000")
                connection.execute("PRAGMA foreign_keys=ON")
                connection.executescript(_SCHEMA)
                connection.commit()
            except sqlite3.Error as exc:
                raise StorageError(
                    f"cannot initialise task database {self.database_path}: {exc}"
                ) from exc
            self._conn = connection
            LOGGER.debug("task store initialised at %s", self.database_path)

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self.initialize()
        assert self._conn is not None  # initialize() either sets it or raises
        return self._conn

    # -- serialisation -----------------------------------------------------
    def _columns(self, record: TaskRecord) -> dict[str, Any]:
        return {
            "id": record.id,
            "name": record.name,
            "status": record.status,
            "created_at": _utc_iso(record.created_at),
            "queued_at": _utc_iso(record.queued_at),
            "started_at": _utc_iso(record.started_at),
            "finished_at": _utc_iso(record.finished_at),
            "timeout_ms": record.timeout_ms,
            "cancel_requested_at": _utc_iso(record.cancel_requested_at),
            "error_json": None if record.error is None else _json_dumps(record.error),
            "steps_json": _json_dumps([_model_json(step) for step in record.steps]),
            "step_results_json": _json_dumps(
                [_model_json(result) for result in record.step_results]
            ),
            "metadata_json": _json_dumps(record.metadata),
            "client_request_id": record.client_request_id,
        }

    @staticmethod
    def _from_row(row: sqlite3.Row) -> TaskRecord:
        return TaskRecord.model_validate(
            {
                "id": row["id"],
                "name": row["name"],
                "status": row["status"],
                "created_at": row["created_at"],
                "queued_at": row["queued_at"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "timeout_ms": row["timeout_ms"],
                "cancel_requested_at": row["cancel_requested_at"],
                "error": _json_loads(row["error_json"], None),
                "steps": _json_loads(row["steps_json"], []),
                "step_results": _json_loads(row["step_results_json"], []),
                "metadata": _json_loads(row["metadata_json"], {}),
                "client_request_id": row["client_request_id"],
            }
        )

    # -- writes ------------------------------------------------------------
    def insert(self, record: TaskRecord) -> TaskRecord:
        """Insert (or replace) a task record."""

        self._upsert(record)
        return record

    def update(self, record: TaskRecord) -> TaskRecord:
        """Persist every column of ``record``."""

        self._upsert(record)
        return record

    def _upsert(self, record: TaskRecord) -> None:
        columns = self._columns(record)
        placeholders = ", ".join("?" for _ in COLUMNS)
        assignments = ", ".join(
            f"{column}=excluded.{column}" for column in COLUMNS if column != "id"
        )
        sql = (
            f"INSERT INTO tasks ({', '.join(COLUMNS)}) VALUES ({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {assignments}"
        )
        values = tuple(columns[column] for column in COLUMNS)
        with self._lock:
            connection = self._connection()
            try:
                with connection:
                    connection.execute(sql, values)
            except sqlite3.Error as exc:
                raise StorageError(
                    f"cannot save task {record.id}: {exc}",
                    details={"task_id": record.id},
                ) from exc

    def update_fields(self, task_id: str, fields: Mapping[str, Any]) -> bool:
        """Update only the given record attributes (avoids lost updates).

        Keys are :class:`TaskRecord` attribute names (``status``, ``started_at``,
        ``step_results``, ``error``, ``cancel_requested_at`` ...). Returns
        ``False`` when the task does not exist.
        """

        columns: dict[str, Any] = {}
        for key, value in fields.items():
            if key in _JSON_FIELDS:
                column = _JSON_FIELDS[key]
                if key == "error":
                    columns[column] = None if value is None else _json_dumps(value)
                elif key == "steps" or key == "step_results":
                    columns[column] = _json_dumps([_model_json(item) for item in value or []])
                else:
                    columns[column] = _json_dumps(value if value is not None else {})
            elif key in _DATETIME_FIELDS:
                columns[key] = _utc_iso(value)
            elif key in COLUMNS:
                columns[key] = value
            else:
                raise ValueError(f"unknown task field: {key!r}")
        if not columns:
            return self.get(task_id) is not None
        assignment = ", ".join(f"{column}=?" for column in columns)
        values = (*columns.values(), task_id)
        sql = f"UPDATE tasks SET {assignment} WHERE id=?"
        with self._lock:
            connection = self._connection()
            try:
                with connection:
                    cursor = connection.execute(sql, values)
                    return cursor.rowcount > 0
            except sqlite3.Error as exc:
                raise StorageError(
                    f"cannot update task {task_id}: {exc}", details={"task_id": task_id}
                ) from exc

    # -- reads -------------------------------------------------------------
    def get(self, task_id: str) -> TaskRecord | None:
        with self._lock:
            connection = self._connection()
            try:
                row = connection.execute(
                    "SELECT * FROM tasks WHERE id=?", (task_id,)
                ).fetchone()
            except sqlite3.Error as exc:
                raise StorageError(
                    f"cannot read task {task_id}: {exc}", details={"task_id": task_id}
                ) from exc
        return None if row is None else self._from_row(row)

    def list(
        self,
        *,
        status: str | TaskStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[TaskRecord], int]:
        """Return one page of tasks (newest first) and the total match count."""

        limit = max(1, min(int(limit), 1000))
        offset = max(0, int(offset))
        where = ""
        params: list[Any] = []
        if status is not None:
            where = " WHERE status=?"
            params.append(str(status))
        with self._lock:
            connection = self._connection()
            try:
                total_row = connection.execute(
                    f"SELECT COUNT(*) AS count FROM tasks{where}", params
                ).fetchone()
                total = int(total_row["count"]) if total_row is not None else 0
                rows = connection.execute(
                    f"SELECT * FROM tasks{where} ORDER BY created_at DESC, rowid DESC "
                    "LIMIT ? OFFSET ?",
                    [*params, limit, offset],
                ).fetchall()
            except sqlite3.Error as exc:
                raise StorageError(f"cannot list tasks: {exc}") from exc
        return [self._from_row(row) for row in rows], total

    def latest(self) -> TaskRecord | None:
        """Most recently created task (diagnostics helper)."""

        with self._lock:
            connection = self._connection()
            try:
                row = connection.execute(
                    "SELECT * FROM tasks ORDER BY created_at DESC, rowid DESC LIMIT 1"
                ).fetchone()
            except sqlite3.Error as exc:
                raise StorageError(f"cannot read latest task: {exc}") from exc
        return None if row is None else self._from_row(row)

    def counts(self) -> dict[str, int]:
        """Task counts per status plus ``total``."""

        counts: dict[str, int] = dict.fromkeys(ALL_STATUSES, 0)
        with self._lock:
            connection = self._connection()
            try:
                rows = connection.execute(
                    "SELECT status, COUNT(*) AS count FROM tasks GROUP BY status"
                ).fetchall()
            except sqlite3.Error as exc:
                raise StorageError(f"cannot count tasks: {exc}") from exc
        total = 0
        for row in rows:
            status = str(row["status"])
            amount = int(row["count"])
            counts[status] = counts.get(status, 0) + amount
            total += amount
        counts["total"] = total
        return counts

    # -- maintenance -------------------------------------------------------
    def recover_interrupted(self) -> int:
        """Mark ``queued``/``running`` tasks as ``interrupted`` (startup recovery)."""

        now = _utc_iso(utc_now())
        error = _json_dumps(
            {
                "code": "TASK_INTERRUPTED",
                "message": "task was interrupted by a service restart",
                "details": {},
            }
        )
        with self._lock:
            connection = self._connection()
            try:
                with connection:
                    cursor = connection.execute(
                        "UPDATE tasks SET status='interrupted', finished_at=?, error_json=? "
                        "WHERE status IN ('queued', 'running')",
                        (now, error),
                    )
                    recovered = int(cursor.rowcount)
            except sqlite3.Error as exc:
                raise StorageError(f"cannot recover interrupted tasks: {exc}") from exc
        if recovered:
            LOGGER.warning("marked %d task(s) as interrupted after restart", recovered)
        return recovered

    def cleanup(
        self,
        *,
        retention_days: int | None = None,
        max_records: int | None = None,
    ) -> int:
        """Delete old tasks according to the configured retention policy."""

        if retention_days is None:
            retention_days = self.config.tasks.retention_days
        if max_records is None:
            max_records = self.config.tasks.max_records
        deleted = 0
        with self._lock:
            connection = self._connection()
            try:
                with connection:
                    if retention_days and retention_days > 0:
                        cutoff = utc_now() - timedelta(days=int(retention_days))
                        cursor = connection.execute(
                            "DELETE FROM tasks WHERE created_at < ? AND status NOT IN "
                            "('queued', 'running')",
                            (_utc_iso(cutoff),),
                        )
                        deleted += int(cursor.rowcount)
                    if max_records and max_records > 0:
                        cursor = connection.execute(
                            "DELETE FROM tasks WHERE id IN ("
                            "SELECT id FROM tasks ORDER BY created_at DESC, rowid DESC LIMIT -1 OFFSET ?"
                            ")",
                            (int(max_records),),
                        )
                        deleted += int(cursor.rowcount)
            except sqlite3.Error as exc:
                raise StorageError(f"cannot clean up tasks: {exc}") from exc
        if deleted:
            LOGGER.info("cleaned up %d old task record(s)", deleted)
        return deleted

    def iter_all(self) -> Iterable[TaskRecord]:
        """Iterate every stored task (diagnostics / tests)."""

        with self._lock:
            connection = self._connection()
            try:
                rows = connection.execute(
                    "SELECT * FROM tasks ORDER BY created_at ASC, rowid ASC"
                ).fetchall()
            except sqlite3.Error as exc:
                raise StorageError(f"cannot iterate tasks: {exc}") from exc
        return [self._from_row(row) for row in rows]


__all__ = ["ALL_STATUSES", "COLUMNS", "TaskStore"]
