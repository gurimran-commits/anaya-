"""Tests for SQLite-backed TaskStore persistence."""

import concurrent.futures
from datetime import datetime, timedelta, timezone

import pytest

from app.tasks.models import (
    Task,
    TaskNotFoundError,
    TaskPriority,
    TaskStatus,
    TaskStoreError,
)
from app.tasks.task_store import SCHEMA_VERSION, TaskStore


@pytest.fixture
def store(tmp_path):
    """Fixture providing an isolated TaskStore instance."""
    db_file = tmp_path / "test_tasks.db"
    store_inst = TaskStore(db_path=db_file)
    yield store_inst
    store_inst.close()


class TestTaskStore:
    """Test suite for SQLite TaskStore operations and concurrency."""

    def test_database_initialization(self, store):
        assert store.db_path.is_file()
        conn = store._get_connection()
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        assert row["version"] == SCHEMA_VERSION

    def test_crud_lifecycle(self, store):
        now = datetime.now(timezone.utc)
        task = Task(
            id="t1234567",
            title="Submit report",
            description="Quarterly review",
            due_date=now + timedelta(days=1),
        )

        # 1. Add
        added = store.add_task(task)
        assert added.id == "t1234567"

        # 2. Get
        retrieved = store.get_task("t1234567")
        assert retrieved is not None
        assert retrieved.title == "Submit report"
        assert retrieved.status == TaskStatus.PENDING.value

        # 3. Update
        updated_task = Task(
            id="t1234567",
            title="Submit updated report",
            status=TaskStatus.IN_PROGRESS.value,
            priority=TaskPriority.HIGH.value,
        )
        store.update_task(updated_task)
        retrieved_updated = store.get_task("t1234567")
        assert retrieved_updated.title == "Submit updated report"
        assert retrieved_updated.status == TaskStatus.IN_PROGRESS.value
        assert retrieved_updated.priority == TaskPriority.HIGH.value

        # 4. Delete
        assert store.delete_task("t1234567") is True
        assert store.get_task("t1234567") is None
        assert store.delete_task("t1234567") is False

    def test_persistence_across_store_reopen(self, tmp_path):
        db_file = tmp_path / "persistent.db"
        store1 = TaskStore(db_path=db_file)
        task = Task(id="t9999999", title="Persisted task")
        store1.add_task(task)
        store1.close()

        # Reopen same database file
        store2 = TaskStore(db_path=db_file)
        retrieved = store2.get_task("t9999999")
        assert retrieved is not None
        assert retrieved.title == "Persisted task"
        store2.close()

    def test_duplicate_task_id_rejected(self, store):
        task1 = Task(id="same-id-1", title="Task 1")
        task2 = Task(id="same-id-1", title="Task 2")
        store.add_task(task1)
        with pytest.raises(TaskStoreError, match="already exists"):
            store.add_task(task2)

    def test_update_non_existent_task_raises_error(self, store):
        task = Task(id="missing-id", title="Missing")
        with pytest.raises(TaskNotFoundError):
            store.update_task(task)

    def test_listing_and_filtering(self, store):
        now = datetime.now(timezone.utc)
        store.add_task(Task(id="task-01", title="Task 1", status=TaskStatus.PENDING.value, priority=TaskPriority.LOW.value))
        store.add_task(Task(id="task-02", title="Task 2", status=TaskStatus.COMPLETED.value, priority=TaskPriority.HIGH.value))
        store.add_task(Task(id="task-03", title="Task 3", status=TaskStatus.PENDING.value, priority=TaskPriority.HIGH.value))

        all_tasks = store.list_tasks()
        assert len(all_tasks) == 3

        pending_tasks = store.list_tasks(status="pending")
        assert len(pending_tasks) == 2
        assert {t.id for t in pending_tasks} == {"task-01", "task-03"}

        high_priority = store.list_tasks(priority="high")
        assert len(high_priority) == 2
        assert {t.id for t in high_priority} == {"task-02", "task-03"}

        pending_high = store.list_tasks(status="pending", priority="high")
        assert len(pending_high) == 1
        assert pending_high[0].id == "task-03"

    def test_search_tasks_by_query(self, store):
        store.add_task(Task(id="t-search-1", title="Call client for contract"))
        store.add_task(Task(id="t-search-2", title="Buy groceries", description="Apples, oranges"))
        store.add_task(Task(id="t-search-3", title="Review contract details"))

        results = store.search_tasks("contract")
        assert len(results) == 2
        assert {t.id for t in results} == {"t-search-1", "t-search-3"}

        desc_results = store.search_tasks("apples")
        assert len(desc_results) == 1
        assert desc_results[0].id == "t-search-2"

        empty_results = store.search_tasks("")
        assert empty_results == []

    def test_due_reminders_and_atomic_claim(self, store):
        now = datetime.now(timezone.utc)
        past = now - timedelta(minutes=5)
        future = now + timedelta(minutes=10)

        t_due = Task(id="due-task-1", title="Due task", reminder_at=past, status=TaskStatus.PENDING.value)
        t_future = Task(id="future-task", title="Future task", reminder_at=future, status=TaskStatus.PENDING.value)
        t_completed = Task(id="done-task", title="Done task", reminder_at=past, status=TaskStatus.COMPLETED.value)

        store.add_task(t_due)
        store.add_task(t_future)
        store.add_task(t_completed)

        due_list = store.get_due_reminders(now)
        assert len(due_list) == 1
        assert due_list[0].id == "due-task-1"

        # Claim the reminder atomically
        claimed = store.claim_reminder("due-task-1", now)
        assert claimed is True

        # Second claim returns False (duplicate prevention)
        claimed_again = store.claim_reminder("due-task-1", now)
        assert claimed_again is False

        # No longer due
        assert store.get_due_reminders(now) == []

    def test_concurrent_access_threads(self, store):
        def insert_task(index: int):
            task = Task(id=f"concurrent-{index:04d}", title=f"Concurrent task {index}")
            store.add_task(task)
            return store.get_task(f"concurrent-{index:04d}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(insert_task, i) for i in range(20)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        assert len(results) == 20
        assert store.count_tasks() == 20
