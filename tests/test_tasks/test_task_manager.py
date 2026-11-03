"""Tests for TaskManager domain logic and lifecycle management."""

from datetime import datetime, timedelta, timezone

import pytest

from app.tasks.models import (
    TaskNotFoundError,
    TaskPriority,
    TaskStatus,
    TaskValidationError,
)
from app.tasks.task_manager import TaskManager
from app.tasks.task_store import TaskStore


@pytest.fixture
def fake_now():
    return datetime(2026, 11, 4, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def manager(tmp_path, fake_now):
    store = TaskStore(db_path=tmp_path / "mgr_tasks.db")
    mgr = TaskManager(store=store, clock=lambda: fake_now)
    yield mgr
    mgr.close()


class TestTaskManager:
    """Test suite for TaskManager operations and domain rules."""

    def test_create_task_generates_short_id(self, manager):
        task = manager.create_task(title="Prepare slides")
        assert len(task.id) == 8
        assert task.title == "Prepare slides"
        assert task.status == TaskStatus.PENDING.value
        assert task.priority == TaskPriority.MEDIUM.value
        assert task.created_at.year == 2026

    def test_due_date_versus_reminder_separation(self, manager, fake_now):
        # 1. Deadline only
        t_deadline = manager.create_task(title="Project deadline", due_date="tomorrow")
        assert t_deadline.due_date is not None
        assert t_deadline.reminder_at is None

        # 2. Reminder only
        t_reminder = manager.create_task(title="Call doctor", reminder_at="5pm")
        assert t_reminder.due_date is None
        assert t_reminder.reminder_at is not None

        # 3. Both specified distinctly
        t_both = manager.create_task(
            title="Submit paper",
            due_date="friday",
            reminder_at="tomorrow at 10am",
        )
        assert t_both.due_date is not None
        assert t_both.reminder_at is not None
        assert t_both.due_date != t_both.reminder_at

    def test_create_task_empty_title_fails(self, manager):
        with pytest.raises(TaskValidationError, match="cannot be empty"):
            manager.create_task(title="")

    def test_get_and_not_found(self, manager):
        task = manager.create_task(title="Sample")
        found = manager.get_task(task.id)
        assert found.id == task.id

        with pytest.raises(TaskNotFoundError):
            manager.get_task("nonexistent")

        with pytest.raises(TaskValidationError):
            manager.get_task("")

    def test_complete_task_lifecycle(self, manager, fake_now):
        task = manager.create_task(title="Incomplete task")
        assert task.status == TaskStatus.PENDING.value
        assert task.completed_at is None

        completed = manager.complete_task(task.id)
        assert completed.status == TaskStatus.COMPLETED.value
        assert completed.completed_at == fake_now

    def test_cancel_task_lifecycle(self, manager):
        task = manager.create_task(title="Meeting")
        cancelled = manager.cancel_task(task.id)
        assert cancelled.status == TaskStatus.CANCELLED.value

    def test_update_resets_reminder_triggered(self, manager, fake_now):
        task = manager.create_task(title="Initial", reminder_at="tomorrow at 9am")
        # Simulate reminder claimed/triggered
        manager.claim_reminder(task.id)
        t_claimed = manager.get_task(task.id)
        assert t_claimed.reminder_triggered is True

        # Update reminder to a new time
        updated = manager.update_task(task.id, reminder_at="friday at 9am")
        assert updated.reminder_triggered is False

    def test_delete_task(self, manager):
        task = manager.create_task(title="To be deleted")
        assert manager.delete_task(task.id) is True

        with pytest.raises(TaskNotFoundError):
            manager.delete_task(task.id)

    def test_search_and_list_tasks(self, manager):
        manager.create_task(title="Clean desk", priority="low")
        manager.create_task(title="Buy desk lamp", priority="medium")
        manager.create_task(title="Fix bug", priority="high")

        desk_tasks = manager.search_tasks("desk")
        assert len(desk_tasks) == 2

        high_tasks = manager.list_tasks(priority="high")
        assert len(high_tasks) == 1
        assert high_tasks[0].title == "Fix bug"

    def test_missed_reminders_query(self, manager, fake_now):
        past = fake_now - timedelta(hours=2)
        task = manager.create_task(title="Missed reminder task")
        # Manually update to past reminder
        manager.update_task(task.id, reminder_at=past)

        missed = manager.get_missed_reminders(fake_now)
        assert len(missed) == 1
        assert missed[0].id == task.id
