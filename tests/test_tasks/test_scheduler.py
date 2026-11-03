"""Tests for TaskScheduler in-process reminder worker."""

import time
from datetime import datetime, timedelta, timezone

import pytest

from app.tasks.models import TaskStatus
from app.tasks.scheduler import TaskScheduler
from app.tasks.task_manager import TaskManager
from app.tasks.task_store import TaskStore


class TestTaskScheduler:
    """Test suite for TaskScheduler reminder dispatching and lifecycle."""

    def test_no_thread_created_on_instantiation(self, tmp_path):
        store = TaskStore(db_path=tmp_path / "sched_test.db")
        mgr = TaskManager(store=store)
        scheduler = TaskScheduler(
            task_manager=mgr,
            on_reminder=lambda t: None,
        )

        assert scheduler.is_running is False
        mgr.close()

    def test_start_and_stop_lifecycle(self, tmp_path):
        store = TaskStore(db_path=tmp_path / "sched_test.db")
        mgr = TaskManager(store=store)
        scheduler = TaskScheduler(
            task_manager=mgr,
            on_reminder=lambda t: None,
            poll_interval=0.1,
        )

        scheduler.start()
        assert scheduler.is_running is True

        # Idempotent start
        scheduler.start()
        assert scheduler.is_running is True

        scheduler.stop(timeout=1.0)
        assert scheduler.is_running is False

        # Idempotent stop
        scheduler.stop()
        assert scheduler.is_running is False
        mgr.close()

    def test_run_pending_dispatches_due_reminders(self, tmp_path):
        now = datetime(2026, 11, 4, 15, 0, 0, tzinfo=timezone.utc)
        store = TaskStore(db_path=tmp_path / "sched_test.db")
        mgr = TaskManager(store=store, clock=lambda: now)

        dispatched = []

        def handle_reminder(task):
            dispatched.append(task)

        scheduler = TaskScheduler(
            task_manager=mgr,
            on_reminder=handle_reminder,
            clock=lambda: now,
        )

        # Create 1 due task and 1 future task
        t_due = mgr.create_task(title="Pay bill", reminder_at=now - timedelta(minutes=5))
        mgr.create_task(title="Future bill", reminder_at=now + timedelta(hours=1))

        # First run dispatches exactly 1 task
        count = scheduler.run_pending(now)
        assert count == 1
        assert len(dispatched) == 1
        assert dispatched[0].id == t_due.id

        # Second run finds 0 due tasks (atomic claim duplicate prevention)
        count_second = scheduler.run_pending(now)
        assert count_second == 0
        assert len(dispatched) == 1

        mgr.close()

    def test_callback_exception_isolated(self, tmp_path):
        now = datetime(2026, 11, 4, 15, 0, 0, tzinfo=timezone.utc)
        store = TaskStore(db_path=tmp_path / "sched_test.db")
        mgr = TaskManager(store=store, clock=lambda: now)

        executed_second = []

        def buggy_callback(task):
            if task.title == "Buggy task":
                raise RuntimeError("Callback crashed!")
            executed_second.append(task)

        scheduler = TaskScheduler(
            task_manager=mgr,
            on_reminder=buggy_callback,
            clock=lambda: now,
        )

        mgr.create_task(title="Buggy task", reminder_at=now - timedelta(minutes=10))
        mgr.create_task(title="Healthy task", reminder_at=now - timedelta(minutes=5))

        # Scheduler must process both tasks without crashing on the first exception
        count = scheduler.run_pending(now)
        assert count == 1  # Healthy task succeeded
        assert len(executed_second) == 1
        assert executed_second[0].title == "Healthy task"

        mgr.close()

    def test_completed_task_not_dispatched(self, tmp_path):
        now = datetime(2026, 11, 4, 15, 0, 0, tzinfo=timezone.utc)
        store = TaskStore(db_path=tmp_path / "sched_test.db")
        mgr = TaskManager(store=store, clock=lambda: now)

        dispatched = []
        scheduler = TaskScheduler(
            task_manager=mgr,
            on_reminder=lambda t: dispatched.append(t),
            clock=lambda: now,
        )

        task = mgr.create_task(title="Done task", reminder_at=now - timedelta(minutes=5))
        mgr.complete_task(task.id)

        count = scheduler.run_pending(now)
        assert count == 0
        assert len(dispatched) == 0

        mgr.close()
