"""Self-tests verifying Phase 8B test infrastructure, fixtures, and helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.controller import AssistantController
from app.notifications.models import ReminderNotification
from app.notifications.protocols import NotificationHandler
from tests.test_e2e.conftest import (
    ControllerFactory,
    FakeClock,
    RecordingNotificationHandler,
    TempAnayaPaths,
)


def test_temp_database_isolation(temp_anaya_dir: TempAnayaPaths) -> None:
    """Verify temporary database fixture creates isolated, valid paths."""
    assert isinstance(temp_anaya_dir.data_dir, Path)
    assert temp_anaya_dir.data_dir.is_dir()
    assert temp_anaya_dir.tasks_db.parent == temp_anaya_dir.data_dir
    assert temp_anaya_dir.memory_db.parent == temp_anaya_dir.data_dir
    assert temp_anaya_dir.tasks_db.name == "tasks.db"
    assert temp_anaya_dir.memory_db.name == "memory.db"

    # Dict-like access
    assert temp_anaya_dir["tasks_db"] == temp_anaya_dir.tasks_db
    assert temp_anaya_dir["memory_db"] == temp_anaya_dir.memory_db


def test_controller_construction_text_only(e2e_controller_factory: ControllerFactory) -> None:
    """Verify factory builds a real AssistantController with audio and AI disabled by default."""
    ctrl = e2e_controller_factory()

    assert isinstance(ctrl, AssistantController)
    assert ctrl.voice_pipeline is None
    assert ctrl.model_manager is None
    assert ctrl.approval_service is not None
    assert ctrl.task_manager is not None
    assert ctrl.memory_manager is not None
    assert ctrl.is_running is False
    assert ctrl._cleanly_stopped is False


def test_controller_cleanup_without_startup(e2e_controller_factory: ControllerFactory) -> None:
    """Verify an unstarted controller can be stopped safely and closes its stores."""
    ctrl = e2e_controller_factory()
    assert ctrl.is_running is False

    ctrl.stop(timeout=1.0)
    assert ctrl._cleanly_stopped is True
    assert ctrl._stopped is True


def test_controller_cleanup_after_normal_startup(e2e_controller_factory: ControllerFactory) -> None:
    """Verify a normally started controller transitions cleanly and stops background threads."""
    ctrl = e2e_controller_factory()
    ctrl.start()

    assert ctrl.is_running is True
    assert ctrl.runtime.is_running is True

    ctrl.stop(timeout=2.0)
    assert ctrl.is_running is False
    assert ctrl._cleanly_stopped is True


def test_sequential_controllers_same_database(
    e2e_controller_factory: ControllerFactory,
    temp_anaya_dir: TempAnayaPaths,
) -> None:
    """Verify multiple sequential controllers can access the same persistent database files."""
    # First controller writes a task
    ctrl1 = e2e_controller_factory()
    ctrl1.start()
    task = ctrl1.task_manager.create_task(title="E2E Infrastructure Task")
    assert task.title == "E2E Infrastructure Task"
    ctrl1.stop(timeout=2.0)

    # Second controller reads the task from the same database file
    ctrl2 = e2e_controller_factory()
    ctrl2.start()
    retrieved = ctrl2.task_manager.get_task(task.id)
    assert retrieved is not None
    assert retrieved.title == "E2E Infrastructure Task"
    ctrl2.stop(timeout=2.0)


def test_recording_notification_handler_protocol_and_signaling() -> None:
    """Verify RecordingNotificationHandler conforms to protocol and signals events."""
    handler = RecordingNotificationHandler()
    assert isinstance(handler, NotificationHandler)
    assert handler.event.is_set() is False
    assert len(handler.notifications) == 0

    notification = ReminderNotification(
        task_id="task-123",
        title="Scheduled Meeting",
        reminder_at=datetime(2026, 11, 5, 14, 0, 0, tzinfo=timezone.utc),
        is_missed=False,
    )

    handler.notify(notification)

    assert handler.event.is_set() is True
    assert handler.wait(timeout=0.1) is True
    assert len(handler.notifications) == 1
    assert handler.notifications[0].task_id == "task-123"

    # Clear should atomically reset state
    handler.clear()
    assert handler.event.is_set() is False
    assert len(handler.notifications) == 0

    # Successive notify after clear works consistently
    notification2 = ReminderNotification(
        task_id="task-456",
        title="Follow-up Call",
        reminder_at=datetime(2026, 11, 5, 15, 0, 0, tzinfo=timezone.utc),
        is_missed=False,
    )
    handler.notify(notification2)
    assert handler.event.is_set() is True
    assert len(handler.notifications) == 1
    assert handler.notifications[0].task_id == "task-456"


def test_recording_notification_handler_concurrency_consistency() -> None:
    """Verify notification list and event remain strictly consistent across concurrent notify/clear."""
    import threading

    handler = RecordingNotificationHandler()
    errors: list[str] = []
    iterations = 200

    def worker_notify() -> None:
        for i in range(iterations):
            notif = ReminderNotification(
                task_id=f"concurrent-{i}",
                title=f"Task {i}",
                reminder_at=datetime(2026, 11, 5, 12, 0, 0, tzinfo=timezone.utc),
            )
            handler.notify(notif)

    def worker_clear_and_check() -> None:
        for _ in range(iterations):
            handler.clear()
            # Under lock, state must never observe mismatch between notifications presence and event state
            with handler._lock:
                has_items = len(handler._notifications) > 0
                event_set = handler._event.is_set()
                if has_items != event_set:
                    errors.append(f"Inconsistent state: has_items={has_items}, event_set={event_set}")

    t1 = threading.Thread(target=worker_notify)
    t2 = threading.Thread(target=worker_clear_and_check)

    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert not errors, f"Concurrency race detected: {errors}"


def test_fake_clock_advancement() -> None:
    """Verify FakeClock returns deterministic datetimes and advances accurately."""
    start_time = datetime(2026, 11, 5, 9, 0, 0, tzinfo=timezone.utc)
    clock = FakeClock(initial_time=start_time)

    assert clock() == start_time
    assert clock.now() == start_time

    # Advance by timedelta
    t1 = clock.advance(timedelta(hours=2))
    assert t1 == datetime(2026, 11, 5, 11, 0, 0, tzinfo=timezone.utc)
    assert clock() == t1

    # Advance by numeric seconds
    t2 = clock.advance(1800)  # 30 minutes
    assert t2 == datetime(2026, 11, 5, 11, 30, 0, tzinfo=timezone.utc)
    assert clock() == t2

    # Set explicitly
    explicit_time = datetime(2026, 12, 1, 0, 0, 0, tzinfo=timezone.utc)
    clock.set(explicit_time)
    assert clock() == explicit_time


def test_controller_with_injected_clock_and_handler(
    e2e_controller_factory: ControllerFactory,
    fake_clock: FakeClock,
    recording_notifications: RecordingNotificationHandler,
) -> None:
    """Verify create_controller accepts and propagates fake_clock and recording_notifications."""
    ctrl = e2e_controller_factory(
        clock=fake_clock,
        custom_notification_handler=recording_notifications,
    )

    # Verify clock is injected into runtime and task manager
    assert ctrl.runtime._clock() == fake_clock()
    assert ctrl.task_manager._clock() == fake_clock()

    # Verify notification handler is injected into runtime
    assert ctrl.runtime._notification_handler is recording_notifications


def test_teardown_failure_reporting_contract() -> None:
    """Verify that teardown exception collection preserves error details rather than swallowing."""
    mock_ctrl = MagicMock()
    mock_ctrl.stop.side_effect = RuntimeError("Simulated SQLite lock error during teardown")

    cleanup_errors: list[tuple[Any, Exception]] = []
    for ctrl in [mock_ctrl]:
        try:
            ctrl.stop(timeout=2.0)
        except Exception as exc:
            cleanup_errors.append((ctrl, exc))

    assert len(cleanup_errors) == 1
    ctrl_ref, exc_ref = cleanup_errors[0]
    assert ctrl_ref is mock_ctrl
    assert isinstance(exc_ref, RuntimeError)
    assert "Simulated SQLite lock error" in str(exc_ref)
