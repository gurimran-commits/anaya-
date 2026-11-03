"""SQLite-backed persistence store for Anaya tasks and reminders."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any

from app.tasks.models import (
    Task,
    TaskNotFoundError,
    TaskPriority,
    TaskStatus,
    TaskStoreError,
)

SCHEMA_VERSION = 1


class TaskStore:
    """Thread-safe SQLite store for user tasks, deadlines, and reminders."""

    DEFAULT_DB_DIR = Path.home() / ".anaya"
    DEFAULT_DB_PATH = DEFAULT_DB_DIR / "tasks.db"

    def __init__(self, db_path: Path | str | None = None) -> None:
        if db_path is None:
            self._db_path = self.DEFAULT_DB_PATH
        else:
            self._db_path = Path(db_path).resolve()

        self._lock = RLock()
        self._connection: sqlite3.Connection | None = None
        self._closed = False

        self._init_db()

    @property
    def db_path(self) -> Path:
        """Return the resolved database file path."""
        return self._db_path

    def _ensure_open(self) -> None:
        if self._closed:
            raise TaskStoreError("TaskStore is closed.")

    def _get_connection(self) -> sqlite3.Connection:
        self._ensure_open()
        if self._connection is None:
            try:
                self._db_path.parent.mkdir(parents=True, exist_ok=True)
                if self._db_path.is_dir():
                    raise TaskStoreError(
                        f"Database path '{self._db_path}' is an existing directory, not a file."
                    )
                self._connection = sqlite3.connect(
                    str(self._db_path),
                    check_same_thread=False,
                )
                self._connection.row_factory = sqlite3.Row
                # Configure SQLite pragmas for concurrency, safety, and integrity
                self._connection.execute("PRAGMA journal_mode = WAL;")
                self._connection.execute("PRAGMA synchronous = NORMAL;")
                self._connection.execute("PRAGMA foreign_keys = ON;")
            except (OSError, sqlite3.Error) as exc:
                raise TaskStoreError(
                    f"Failed to open database at '{self._db_path}': {exc}"
                ) from exc
        return self._connection

    def _init_db(self) -> None:
        """Create tables and indexes if they do not exist."""
        with self._lock:
            conn = self._get_connection()
            with conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_version (
                        version INTEGER PRIMARY KEY,
                        applied_at TEXT NOT NULL
                    );
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS tasks (
                        id TEXT PRIMARY KEY,
                        title TEXT NOT NULL,
                        description TEXT,
                        status TEXT NOT NULL,
                        priority TEXT NOT NULL,
                        due_date TEXT,
                        reminder_at TEXT,
                        reminder_triggered INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        completed_at TEXT,
                        metadata TEXT NOT NULL
                    );
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_tasks_due_date ON tasks(due_date);"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_tasks_reminder ON tasks(reminder_at, reminder_triggered, status);"
                )

                # Ensure schema version record exists
                row = conn.execute(
                    "SELECT version FROM schema_version WHERE version = ?",
                    (SCHEMA_VERSION,),
                ).fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                        (SCHEMA_VERSION, datetime.now(timezone.utc).isoformat()),
                    )

    def _row_to_task(self, row: sqlite3.Row) -> Task:
        """Map a database row to an immutable Task instance."""
        due_date = None
        if row["due_date"]:
            due_date = datetime.fromisoformat(row["due_date"])
            if due_date.tzinfo is None:
                due_date = due_date.replace(tzinfo=timezone.utc)

        reminder_at = None
        if row["reminder_at"]:
            reminder_at = datetime.fromisoformat(row["reminder_at"])
            if reminder_at.tzinfo is None:
                reminder_at = reminder_at.replace(tzinfo=timezone.utc)

        created_at = datetime.fromisoformat(row["created_at"])
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)

        updated_at = datetime.fromisoformat(row["updated_at"])
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)

        completed_at = None
        if row["completed_at"]:
            completed_at = datetime.fromisoformat(row["completed_at"])
            if completed_at.tzinfo is None:
                completed_at = completed_at.replace(tzinfo=timezone.utc)

        metadata_dict = {}
        if row["metadata"]:
            try:
                metadata_dict = json.loads(row["metadata"])
            except Exception:
                metadata_dict = {}

        return Task(
            id=row["id"],
            title=row["title"],
            description=row["description"],
            status=row["status"],
            priority=row["priority"],
            due_date=due_date,
            reminder_at=reminder_at,
            reminder_triggered=bool(row["reminder_triggered"]),
            created_at=created_at,
            updated_at=updated_at,
            completed_at=completed_at,
            metadata=metadata_dict,
        )

    def add_task(self, task: Task) -> Task:
        """Insert a new task into the store using parameterized queries."""
        with self._lock:
            conn = self._get_connection()
            try:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO tasks (
                            id, title, description, status, priority,
                            due_date, reminder_at, reminder_triggered,
                            created_at, updated_at, completed_at, metadata
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        (
                            task.id,
                            task.title,
                            task.description,
                            task.status,
                            task.priority,
                            task.due_date.astimezone(timezone.utc).isoformat()
                            if task.due_date
                            else None,
                            task.reminder_at.astimezone(timezone.utc).isoformat()
                            if task.reminder_at
                            else None,
                            1 if task.reminder_triggered else 0,
                            task.created_at.astimezone(timezone.utc).isoformat(),
                            task.updated_at.astimezone(timezone.utc).isoformat(),
                            task.completed_at.astimezone(timezone.utc).isoformat()
                            if task.completed_at
                            else None,
                            json.dumps(task.metadata),
                        ),
                    )
                return task
            except sqlite3.IntegrityError as exc:
                raise TaskStoreError(
                    f"Task with ID '{task.id}' already exists: {exc}"
                ) from exc
            except sqlite3.Error as exc:
                raise TaskStoreError(f"Failed to insert task: {exc}") from exc

    def get_task(self, task_id: str) -> Task | None:
        """Retrieve a task by its unique ID."""
        with self._lock:
            conn = self._get_connection()
            try:
                row = conn.execute(
                    "SELECT * FROM tasks WHERE id = ?;", (task_id,)
                ).fetchone()
                return self._row_to_task(row) if row else None
            except sqlite3.Error as exc:
                raise TaskStoreError(f"Failed to retrieve task: {exc}") from exc

    def list_tasks(
        self,
        status: str | None = None,
        priority: str | None = None,
    ) -> list[Task]:
        """List tasks with optional status and priority filters."""
        query = "SELECT * FROM tasks WHERE 1=1"
        params: list[Any] = []

        if status is not None:
            query += " AND status = ?"
            params.append(status.strip().casefold())
        if priority is not None:
            query += " AND priority = ?"
            params.append(priority.strip().casefold())

        query += " ORDER BY CASE WHEN due_date IS NULL THEN 1 ELSE 0 END, due_date ASC, created_at DESC;"

        with self._lock:
            conn = self._get_connection()
            try:
                rows = conn.execute(query, params).fetchall()
                return [self._row_to_task(r) for r in rows]
            except sqlite3.Error as exc:
                raise TaskStoreError(f"Failed to list tasks: {exc}") from exc

    def search_tasks(
        self,
        query_str: str,
        status: str | None = None,
    ) -> list[Task]:
        """Search tasks by title or description substring."""
        if not query_str or not query_str.strip():
            return []

        pattern = f"%{query_str.strip().casefold()}%"
        sql = """
            SELECT * FROM tasks
            WHERE (LOWER(title) LIKE ? OR LOWER(COALESCE(description, '')) LIKE ?)
        """
        params: list[Any] = [pattern, pattern]

        if status is not None:
            sql += " AND status = ?"
            params.append(status.strip().casefold())

        sql += " ORDER BY created_at DESC;"

        with self._lock:
            conn = self._get_connection()
            try:
                rows = conn.execute(sql, params).fetchall()
                return [self._row_to_task(r) for r in rows]
            except sqlite3.Error as exc:
                raise TaskStoreError(f"Failed to search tasks: {exc}") from exc

    def update_task(self, task: Task) -> Task:
        """Persist updates to an existing task."""
        with self._lock:
            conn = self._get_connection()
            try:
                with conn:
                    cur = conn.execute(
                        """
                        UPDATE tasks SET
                            title = ?,
                            description = ?,
                            status = ?,
                            priority = ?,
                            due_date = ?,
                            reminder_at = ?,
                            reminder_triggered = ?,
                            updated_at = ?,
                            completed_at = ?,
                            metadata = ?
                        WHERE id = ?;
                        """,
                        (
                            task.title,
                            task.description,
                            task.status,
                            task.priority,
                            task.due_date.astimezone(timezone.utc).isoformat()
                            if task.due_date
                            else None,
                            task.reminder_at.astimezone(timezone.utc).isoformat()
                            if task.reminder_at
                            else None,
                            1 if task.reminder_triggered else 0,
                            task.updated_at.astimezone(timezone.utc).isoformat(),
                            task.completed_at.astimezone(timezone.utc).isoformat()
                            if task.completed_at
                            else None,
                            json.dumps(task.metadata),
                            task.id,
                        ),
                    )
                    if cur.rowcount == 0:
                        raise TaskNotFoundError(
                            f"Task with ID '{task.id}' does not exist."
                        )
                return task
            except TaskNotFoundError:
                raise
            except sqlite3.Error as exc:
                raise TaskStoreError(f"Failed to update task: {exc}") from exc

    def delete_task(self, task_id: str) -> bool:
        """Delete a task by its unique ID."""
        with self._lock:
            conn = self._get_connection()
            try:
                with conn:
                    cur = conn.execute(
                        "DELETE FROM tasks WHERE id = ?;", (task_id,)
                    )
                    return cur.rowcount > 0
            except sqlite3.Error as exc:
                raise TaskStoreError(f"Failed to delete task: {exc}") from exc

    def get_due_reminders(self, now: datetime) -> list[Task]:
        """Fetch pending tasks with reminders scheduled at or before the given instant."""
        now_utc = (
            now.astimezone(timezone.utc).isoformat()
            if now.tzinfo
            else now.replace(tzinfo=timezone.utc).isoformat()
        )
        sql = """
            SELECT * FROM tasks
            WHERE reminder_at IS NOT NULL
              AND reminder_at <= ?
              AND reminder_triggered = 0
              AND status = 'pending'
            ORDER BY reminder_at ASC;
        """
        with self._lock:
            conn = self._get_connection()
            try:
                rows = conn.execute(sql, (now_utc,)).fetchall()
                return [self._row_to_task(r) for r in rows]
            except sqlite3.Error as exc:
                raise TaskStoreError(
                    f"Failed to query due reminders: {exc}"
                ) from exc

    def claim_reminder(self, task_id: str, updated_at: datetime) -> bool:
        """Atomically claim a reminder by setting reminder_triggered = 1."""
        upd_str = (
            updated_at.astimezone(timezone.utc).isoformat()
            if updated_at.tzinfo
            else updated_at.replace(tzinfo=timezone.utc).isoformat()
        )
        sql = """
            UPDATE tasks
            SET reminder_triggered = 1,
                updated_at = ?
            WHERE id = ?
              AND reminder_triggered = 0
              AND status = 'pending';
        """
        with self._lock:
            conn = self._get_connection()
            try:
                with conn:
                    cur = conn.execute(sql, (upd_str, task_id))
                    return cur.rowcount > 0
            except sqlite3.Error as exc:
                raise TaskStoreError(f"Failed to claim reminder: {exc}") from exc

    def get_missed_reminders(self, now: datetime) -> list[Task]:
        """Fetch tasks whose reminder_at passed while Anaya was closed or inactive."""
        return self.get_due_reminders(now)

    def count_tasks(self, status: str | None = None) -> int:
        """Return the number of stored tasks, optionally filtered by status."""
        sql = "SELECT COUNT(*) FROM tasks"
        params: list[Any] = []
        if status is not None:
            sql += " WHERE status = ?"
            params.append(status.strip().casefold())

        with self._lock:
            conn = self._get_connection()
            try:
                row = conn.execute(sql, params).fetchone()
                return int(row[0]) if row else 0
            except sqlite3.Error as exc:
                raise TaskStoreError(f"Failed to count tasks: {exc}") from exc

    def close(self) -> None:
        """Close connection cleanly."""
        with self._lock:
            if not self._closed and self._connection is not None:
                try:
                    self._connection.close()
                except sqlite3.Error:
                    pass
                self._connection = None
                self._closed = True
