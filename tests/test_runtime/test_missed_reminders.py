"""Tests for missed reminder recovery on startup, duplicate prevention, and status exclusion."""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.core.config import AppConfig
from app.notifications.models import ReminderNotification
from app.runtime import AnayaRuntime
from app.tasks.models import TaskStatus
from app.tasks.task_manager import TaskManager


@pytest.fixture
def temp_db(tmp_path):
    return tmp_path / "test_missed.db"


def test_startup_recovers_missed_reminders_from_downtime(temp_db):
    """Reminders whose scheduled time passed while Anaya was closed are recovered on startup."""
    # 1. Simulate application run in the past: task scheduled at 10:00
    past_time = datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc)
    mgr = TaskManager(db_path=temp_db, clock=lambda: past_time)
    task = mgr.create_task("Submit quarterly taxes", reminder_at=past_time)
    mgr.close()

    # 2. Reopen application at 14:00 (4 hours later)
    current_time = datetime(2026, 11, 5, 14, 0, tzinfo=timezone.utc)
    recovered: list[ReminderNotification] = []

    runtime = AnayaRuntime(
        db_path=temp_db,
        on_reminder=lambda notif: recovered.append(notif),
        clock=lambda: current_time,
    )
    try:
        runtime.start()
        # Missed reminder must be recovered and dispatched with is_missed=True
        assert len(recovered) == 1
        assert recovered[0].task_id == task.id
        assert recovered[0].is_missed is True
        assert "Missed reminder" in recovered[0].message
    finally:
        runtime.stop()


def test_completed_tasks_are_excluded_from_missed_recovery(temp_db):
    """Completed tasks whose reminder time passed must NOT be triggered on startup."""
    past_time = datetime(2026, 11, 5, 9, 0, tzinfo=timezone.utc)
    mgr = TaskManager(db_path=temp_db, clock=lambda: past_time)
    task = mgr.create_task("Finish sprint review", reminder_at=past_time)
    mgr.complete_task(task.id)
    mgr.close()

    current_time = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
    recovered: list[ReminderNotification] = []

    runtime = AnayaRuntime(
        db_path=temp_db,
        on_reminder=lambda notif: recovered.append(notif),
        clock=lambda: current_time,
    )
    try:
        runtime.start()
        assert len(recovered) == 0
    finally:
        runtime.stop()


def test_cancelled_tasks_are_excluded_from_missed_recovery(temp_db):
    """Cancelled tasks whose reminder time passed must NOT be triggered on startup."""
    past_time = datetime(2026, 11, 5, 9, 0, tzinfo=timezone.utc)
    mgr = TaskManager(db_path=temp_db, clock=lambda: past_time)
    task = mgr.create_task("Call vendor", reminder_at=past_time)
    mgr.cancel_task(task.id)
    mgr.close()

    current_time = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
    recovered: list[ReminderNotification] = []

    runtime = AnayaRuntime(
        db_path=temp_db,
        on_reminder=lambda notif: recovered.append(notif),
        clock=lambda: current_time,
    )
    try:
        runtime.start()
        assert len(recovered) == 0
    finally:
        runtime.stop()


def test_duplicate_missed_recovery_prevented_across_restarts(temp_db):
    """Once recovered and claimed, subsequent restarts do NOT re-deliver the reminder."""
    past_time = datetime(2026, 11, 5, 8, 0, tzinfo=timezone.utc)
    mgr = TaskManager(db_path=temp_db, clock=lambda: past_time)
    mgr.create_task("Weekly backup", reminder_at=past_time)
    mgr.close()

    current_time = datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc)
    recovered_first: list[ReminderNotification] = []

    # First session: recovers and claims reminder
    with AnayaRuntime(
        db_path=temp_db,
        on_reminder=lambda notif: recovered_first.append(notif),
        clock=lambda: current_time,
    ) as rt1:
        assert len(recovered_first) == 1

    # Second session: same time, must NOT re-deliver
    recovered_second: list[ReminderNotification] = []
    with AnayaRuntime(
        db_path=temp_db,
        on_reminder=lambda notif: recovered_second.append(notif),
        clock=lambda: current_time,
    ) as rt2:
        assert len(recovered_second) == 0


def test_missed_recovery_disabled_via_config(temp_db):
    """When task_recover_missed_reminders is False, missed reminders are not recovered."""
    past_time = datetime(2026, 11, 5, 8, 0, tzinfo=timezone.utc)
    mgr = TaskManager(db_path=temp_db, clock=lambda: past_time)
    mgr.create_task("Old reminder", reminder_at=past_time)
    mgr.close()

    config = AppConfig(task_recover_missed_reminders=False)
    recovered: list[ReminderNotification] = []
    with AnayaRuntime(
        config=config,
        db_path=temp_db,
        on_reminder=lambda notif: recovered.append(notif),
        clock=lambda: datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc),
    ) as rt:
        assert len(recovered) == 0


def test_callback_failure_during_missed_recovery_does_not_halt_startup(temp_db):
    """A failing callback during recovery logs the error and continues startup."""
    past_time = datetime(2026, 11, 5, 8, 0, tzinfo=timezone.utc)
    mgr = TaskManager(db_path=temp_db, clock=lambda: past_time)
    mgr.create_task("Task 1", reminder_at=past_time)
    mgr.create_task("Task 2", reminder_at=past_time)
    mgr.close()

    attempted: list[str] = []

    def fragile_cb(notif: ReminderNotification):
        attempted.append(notif.title)
        if notif.title == "Task 1":
            raise RuntimeError("Notification UI crash")

    with AnayaRuntime(
        db_path=temp_db,
        on_reminder=fragile_cb,
        clock=lambda: datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc),
    ) as rt:
        # Both tasks were processed; Task 2 succeeded despite Task 1 throwing
        assert len(attempted) == 2
        assert rt.is_running is True
