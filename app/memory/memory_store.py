import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any

from app.memory.models import Memory


class MemoryStore:
    """SQLite-backed persistence store for Anaya user memories.

    Uses parameterized queries, connection management, and safe schema initialization.
    """

    DEFAULT_DB_DIR = Path.home() / ".anaya"
    DEFAULT_DB_PATH = DEFAULT_DB_DIR / "memory.db"

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
            raise RuntimeError("MemoryStore is closed.")

    def _get_connection(self) -> sqlite3.Connection:
        self._ensure_open()
        if self._connection is None:
            try:
                # Ensure parent directory exists.
                self._db_path.parent.mkdir(parents=True, exist_ok=True)
                if self._db_path.is_dir():
                    raise ValueError(
                        f"Database path '{self._db_path}' is an existing directory, not a file."
                    )
                self._connection = sqlite3.connect(
                    str(self._db_path),
                    check_same_thread=False,
                )
                self._connection.row_factory = sqlite3.Row
                # Configure SQLite pragmas for safety and integrity.
                self._connection.execute("PRAGMA journal_mode = WAL;")
                self._connection.execute("PRAGMA synchronous = NORMAL;")
                self._connection.execute("PRAGMA foreign_keys = ON;")
            except (OSError, sqlite3.Error) as exc:
                raise RuntimeError(
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
                    CREATE TABLE IF NOT EXISTS memories (
                        id TEXT PRIMARY KEY,
                        content TEXT NOT NULL,
                        category TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        tags TEXT NOT NULL,
                        metadata TEXT NOT NULL
                    );
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(category);"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_memories_created_at ON memories(created_at);"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_memories_updated_at ON memories(updated_at);"
                )

    @staticmethod
    def _row_to_memory(row: sqlite3.Row) -> Memory:
        """Convert a database row into an immutable Memory model."""
        tags_raw = json.loads(row["tags"])
        tags_tuple = tuple(tags_raw) if isinstance(tags_raw, list) else ()
        metadata_dict = json.loads(row["metadata"])

        created_dt = datetime.fromisoformat(row["created_at"])
        updated_dt = datetime.fromisoformat(row["updated_at"])

        return Memory(
            id=row["id"],
            content=row["content"],
            category=row["category"],
            created_at=created_dt,
            updated_at=updated_dt,
            tags=tags_tuple,
            metadata=metadata_dict if isinstance(metadata_dict, dict) else {},
        )

    def create(self, memory: Memory) -> Memory:
        """Insert a new memory record."""
        with self._lock:
            self._ensure_open()
            conn = self._get_connection()
            try:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO memories (id, content, category, created_at, updated_at, tags, metadata)
                        VALUES (?, ?, ?, ?, ?, ?, ?);
                        """,
                        (
                            memory.id,
                            memory.content,
                            memory.category,
                            memory.created_at.isoformat(),
                            memory.updated_at.isoformat(),
                            json.dumps(list(memory.tags)),
                            json.dumps(memory.metadata),
                        ),
                    )
                return memory
            except sqlite3.IntegrityError as exc:
                raise ValueError(
                    f"Memory with ID '{memory.id}' already exists."
                ) from exc
            except sqlite3.Error as exc:
                raise RuntimeError(f"Database error creating memory: {exc}") from exc

    def get(self, memory_id: str) -> Memory | None:
        """Retrieve a memory by ID."""
        if not isinstance(memory_id, str) or not memory_id.strip():
            return None

        with self._lock:
            self._ensure_open()
            conn = self._get_connection()
            cursor = conn.execute(
                "SELECT * FROM memories WHERE id = ? LIMIT 1;",
                (memory_id.strip(),),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return self._row_to_memory(row)

    def list_all(
        self,
        category: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[Memory, ...]:
        """List stored memories ordered by updated_at descending."""
        if not isinstance(limit, int) or limit <= 0:
            limit = 100
        if not isinstance(offset, int) or offset < 0:
            offset = 0

        with self._lock:
            self._ensure_open()
            conn = self._get_connection()
            if category:
                cursor = conn.execute(
                    """
                    SELECT * FROM memories
                    WHERE category = ?
                    ORDER BY updated_at DESC
                    LIMIT ? OFFSET ?;
                    """,
                    (category.strip().casefold(), limit, offset),
                )
            else:
                cursor = conn.execute(
                    """
                    SELECT * FROM memories
                    ORDER BY updated_at DESC
                    LIMIT ? OFFSET ?;
                    """,
                    (limit, offset),
                )
            return tuple(self._row_to_memory(row) for row in cursor.fetchall())

    def update(self, memory: Memory) -> Memory:
        """Update an existing memory record."""
        with self._lock:
            self._ensure_open()
            conn = self._get_connection()
            try:
                with conn:
                    cursor = conn.execute(
                        """
                        UPDATE memories
                        SET content = ?, category = ?, updated_at = ?, tags = ?, metadata = ?
                        WHERE id = ?;
                        """,
                        (
                            memory.content,
                            memory.category,
                            memory.updated_at.isoformat(),
                            json.dumps(list(memory.tags)),
                            json.dumps(memory.metadata),
                            memory.id,
                        ),
                    )
                if cursor.rowcount == 0:
                    raise KeyError(f"Memory with ID '{memory.id}' not found.")
                return memory
            except sqlite3.Error as exc:
                raise RuntimeError(f"Database error updating memory: {exc}") from exc

    def delete(self, memory_id: str) -> bool:
        """Delete a memory record by ID."""
        if not isinstance(memory_id, str) or not memory_id.strip():
            return False

        with self._lock:
            self._ensure_open()
            conn = self._get_connection()
            try:
                with conn:
                    cursor = conn.execute(
                        "DELETE FROM memories WHERE id = ?;",
                        (memory_id.strip(),),
                    )
                return cursor.rowcount > 0
            except sqlite3.Error as exc:
                raise RuntimeError(f"Database error deleting memory: {exc}") from exc

    def clear(self) -> int:
        """Delete all memory records."""
        with self._lock:
            self._ensure_open()
            conn = self._get_connection()
            try:
                with conn:
                    cursor = conn.execute("DELETE FROM memories;")
                return cursor.rowcount
            except sqlite3.Error as exc:
                raise RuntimeError(f"Database error clearing memories: {exc}") from exc

    def search(
        self,
        query: str,
        category: str | None = None,
        limit: int = 100,
    ) -> tuple[Memory, ...]:
        """Search memories using parameterized text and tag matching."""
        if not isinstance(query, str) or not query.strip():
            return ()
        if not isinstance(limit, int) or limit <= 0:
            limit = 100

        search_pattern = f"%{query.strip()}%"

        with self._lock:
            self._ensure_open()
            conn = self._get_connection()
            if category:
                cursor = conn.execute(
                    """
                    SELECT * FROM memories
                    WHERE category = ?
                      AND (content LIKE ? OR tags LIKE ?)
                    ORDER BY updated_at DESC
                    LIMIT ?;
                    """,
                    (category.strip().casefold(), search_pattern, search_pattern, limit),
                )
            else:
                cursor = conn.execute(
                    """
                    SELECT * FROM memories
                    WHERE content LIKE ? OR tags LIKE ? OR category LIKE ?
                    ORDER BY updated_at DESC
                    LIMIT ?;
                    """,
                    (search_pattern, search_pattern, search_pattern, limit),
                )
            return tuple(self._row_to_memory(row) for row in cursor.fetchall())

    def close(self) -> None:
        """Close database connection."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._connection is not None:
                try:
                    self._connection.close()
                except sqlite3.Error:
                    pass
                self._connection = None

    def __enter__(self) -> "MemoryStore":
        self._ensure_open()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()
