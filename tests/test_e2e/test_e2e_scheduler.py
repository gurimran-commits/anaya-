"""End-to-end validation for task reminder scheduling, notifications, and recovery."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.core.config import AppConfig
from app.notifications.handlers import CallbackNotificationHandler
from app.notifications.models import ReminderNotification
from app.tasks.models import TaskPriority, TaskStatus
from tests.test_e2e.conftest import (
    ControllerFactory,
    FakeClock,
    RecordingNotificationHandler,
)


def test_due_reminder_delivery(
    e2e_controller_factory: ControllerFactory,
    fake_clock: FakeClock,
    recording_notifications: RecordingNotificationHandler,
) -> None:
    """Scenario 1: Due reminder scheduled at or before current time is claimed and delivered."""
    ctrl = e2e_controller_factory(
        clock=fake_clock,
        custom_notification_handler=recording_notifications,
    )

    # Schedule reminder 5 minutes in the past relative to fake clock
    past_reminder = fake_clock.now() - timedelta(minutes=5)
    task = ctrl.task_manager.create_task(
        title="Submit Timesheet",
        reminder_at=past_reminder,
        priority=TaskPriority.HIGH,
    )

    assert len(recording_notifications.notifications) == 0

    # Deterministic synchronous scheduler tick
    dispatched = ctrl.runtime.run_scheduler_pending()
    assert dispatched == 1

    # Verify notification received and synchronization event signaled
    assert recording_notifications.event.is_set() is True
    assert len(recording_notifications.notifications) == 1

    notification = recording_notifications.notifications[0]
    assert notification.task_id == task.id
    assert notification.title == "Submit Timesheet"
    assert notification.priority == TaskPriority.HIGH.value
    assert notification.is_missed is False
    assert "Reminder: 'Submit Timesheet' is due now" in notification.message

    ctrl.stop(timeout=2.0)


def test_future_reminder_timing(
    e2e_controller_factory: ControllerFactory,
    fake_clock: FakeClock,
    recording_notifications: RecordingNotificationHandler,
) -> None:
    """Scenario 2: Future reminders are not delivered early, but fire once clock advances."""
    ctrl = e2e_controller_factory(
        clock=fake_clock,
        custom_notification_handler=recording_notifications,
    )

    # Schedule reminder 2 hours in the future
    future_reminder = fake_clock.now() + timedelta(hours=2)
    task = ctrl.task_manager.create_task(
        title="Upcoming Executive Review",
        reminder_at=future_reminder,
    )

    # Run scheduler: must NOT deliver early
    dispatched = ctrl.runtime.run_scheduler_pending()
    assert dispatched == 0
    assert len(recording_notifications.notifications) == 0
    assert recording_notifications.event.is_set() is False

    # Advance clock past the scheduled reminder time
    fake_clock.advance(timedelta(hours=2, minutes=10))

    # Run scheduler again: reminder is now due and delivered
    dispatched_later = ctrl.runtime.run_scheduler_pending()
    assert dispatched_later == 1
    assert len(recording_notifications.notifications) == 1
    assert recording_notifications.notifications[0].task_id == task.id

    ctrl.stop(timeout=2.0)


def test_no_duplicate_delivery_at_most_once(
    e2e_controller_factory: ControllerFactory,
    fake_clock: FakeClock,
    recording_notifications: RecordingNotificationHandler,
) -> None:
    """Scenario 3: At-most-once delivery: repeated scheduler ticks do not deliver already claimed reminders."""
    ctrl = e2e_controller_factory(
        clock=fake_clock,
        custom_notification_handler=recording_notifications,
    )

    due_time = fake_clock.now() - timedelta(minutes=1)
    task = ctrl.task_manager.create_task(
        title="Single-fire Critical Alert",
        reminder_at=due_time,
    )

    # First tick claims and dispatches
    assert ctrl.runtime.run_scheduler_pending() == 1
    assert len(recording_notifications.notifications) == 1

    # Second tick: already claimed, at-most-once delivery guaranteed
    assert ctrl.runtime.run_scheduler_pending() == 0
    assert len(recording_notifications.notifications) == 1

    # Third tick: still no duplicate
    assert ctrl.runtime.run_scheduler_pending() == 0
    assert len(recording_notifications.notifications) == 1

    # Verify task state in database: reminder_triggered is set to True
    reloaded_task = ctrl.task_manager.get_task(task.id)
    assert reloaded_task.reminder_triggered is True

    ctrl.stop(timeout=2.0)


def test_multiple_due_reminders_delivery(
    e2e_controller_factory: ControllerFactory,
    fake_clock: FakeClock,
    recording_notifications: RecordingNotificationHandler,
) -> None:
    """Scenario 4: Multiple simultaneously due reminders are all delivered accurately."""
    ctrl = e2e_controller_factory(
        clock=fake_clock,
        custom_notification_handler=recording_notifications,
    )

    now = fake_clock.now()
    t1 = ctrl.task_manager.create_task(title="Batch Task 1", reminder_at=now - timedelta(minutes=10))
    t2 = ctrl.task_manager.create_task(title="Batch Task 2", reminder_at=now - timedelta(minutes=5))
    t3 = ctrl.task_manager.create_task(title="Batch Task 3", reminder_at=now)

    # Run scheduler: all 3 should be claimed and dispatched
    dispatched = ctrl.runtime.run_scheduler_pending()
    assert dispatched == 3
    assert len(recording_notifications.notifications) == 3

    delivered_ids = {n.task_id for n in recording_notifications.notifications}
    assert delivered_ids == {t1.id, t2.id, t3.id}

    ctrl.stop(timeout=2.0)


def test_missed_reminder_recovery_on_startup(
    e2e_controller_factory: ControllerFactory,
    fake_clock: FakeClock,
) -> None:
    """Scenario 5: Reminders that become due during downtime are recovered upon startup."""
    # Session 1: Create task with reminder scheduled in the future
    ctrl1 = e2e_controller_factory(clock=fake_clock)
    ctrl1.start()

    reminder_time = fake_clock.now() + timedelta(hours=1)
    task = ctrl1.task_manager.create_task(
        title="Offline System Maintenance",
        reminder_at=reminder_time,
    )
    ctrl1.stop(timeout=2.0)

    # Application is offline for 4 hours while reminder_time passes
    fake_clock.advance(timedelta(hours=4))

    # Session 2: Reopen application on the same database
    recording2 = RecordingNotificationHandler()
    ctrl2 = e2e_controller_factory(
        clock=fake_clock,
        custom_notification_handler=recording2,
    )

    # Calling start() executes recover_missed_reminders()
    ctrl2.start()

    assert len(recording2.notifications) == 1
    missed_notif = recording2.notifications[0]
    assert missed_notif.task_id == task.id
    assert missed_notif.title == "Offline System Maintenance"
    assert missed_notif.is_missed is True
    assert "Missed reminder for task 'Offline System Maintenance'" in missed_notif.message

    # Ensure subsequent ticks do not dispatch it again
    assert ctrl2.runtime.run_scheduler_pending() == 0

    ctrl2.stop(timeout=2.0)


def test_terminal_tasks_excluded_from_reminders(
    e2e_controller_factory: ControllerFactory,
    fake_clock: FakeClock,
    recording_notifications: RecordingNotificationHandler,
) -> None:
    """Scenario 6: Completed or cancelled tasks are excluded from reminder generation."""
    ctrl = e2e_controller_factory(
        clock=fake_clock,
        custom_notification_handler=recording_notifications,
    )

    due_time = fake_clock.now() - timedelta(minutes=15)

    # Create task and complete it
    task_done = ctrl.task_manager.create_task(title="Finished Task", reminder_at=due_time)
    ctrl.task_manager.complete_task(task_done.id)

    # Create task and cancel it
    task_cancelled = ctrl.task_manager.create_task(title="Aborted Task", reminder_at=due_time)
    ctrl.task_manager.cancel_task(task_cancelled.id)

    # Run scheduler: neither completed nor cancelled tasks should trigger
    dispatched = ctrl.runtime.run_scheduler_pending()
    assert dispatched == 0
    assert len(recording_notifications.notifications) == 0

    ctrl.stop(timeout=2.0)


def test_scheduler_thread_lifecycle_and_background_dispatch(
    e2e_controller_factory: ControllerFactory,
    fake_clock: FakeClock,
    recording_notifications: RecordingNotificationHandler,
) -> None:
    """Scenario 7: Background worker thread executes poll loop, signals event, and stops cleanly."""
    fast_poll_config = AppConfig(
        task_scheduler_poll_interval=0.05,
    )

    ctrl = e2e_controller_factory(
        config=fast_poll_config,
        clock=fake_clock,
        custom_notification_handler=recording_notifications,
    )

    # Create a task due in the past
    task = ctrl.task_manager.create_task(
        title="Background Worker Test Task",
        reminder_at=fake_clock.now() - timedelta(minutes=1),
    )

    # Start controller: boots runtime and AnayaTaskSchedulerWorker background thread
    ctrl.start()
    assert ctrl.runtime.scheduler.is_running is True

    # Idempotent start check
    ctrl.runtime.scheduler.start()
    assert ctrl.runtime.scheduler.is_running is True

    # Synchronize with background thread using event wait (zero arbitrary sleeps)
    received = recording_notifications.wait(timeout=2.0)
    assert received is True
    assert len(recording_notifications.notifications) == 1
    assert recording_notifications.notifications[0].task_id == task.id

    # Clean shutdown joins thread within timeout
    ctrl.stop(timeout=2.0)
    assert ctrl.runtime.scheduler.is_running is False
    assert ctrl._cleanly_stopped is True

    # Idempotent stop check
    ctrl.runtime.scheduler.stop(timeout=1.0)
    assert ctrl.runtime.scheduler.is_running is False


def test_scheduler_worker_hang_preserves_task_store(
    e2e_controller_factory: ControllerFactory,
    fake_clock: FakeClock,
) -> None:
    """Scenario 7 (cont): TaskStore is preserved open if worker thread exceeds shutdown timeout (Phase 7B)."""
    entered_callback = threading.Event()
    unblock_callback = threading.Event()

    def _blocking_notification_handler(notif: ReminderNotification) -> None:
        entered_callback.set()
        unblock_callback.wait(timeout=3.0)

    # Disable missed reminder recovery on start so task is picked up by the background scheduler worker
    config = AppConfig(
        task_recover_missed_reminders=False,
        task_scheduler_poll_interval=0.05,
    )

    ctrl = e2e_controller_factory(
        config=config,
        clock=fake_clock,
        custom_notification_handler=CallbackNotificationHandler(_blocking_notification_handler),
    )

    try:
        # Create due task to trigger the blocking callback in background worker
        ctrl.task_manager.create_task(
            title="Hanging Worker Task",
            reminder_at=fake_clock.now() - timedelta(minutes=1),
        )

        ctrl.start()

        # Wait until background worker enters the callback
        assert entered_callback.wait(timeout=2.0) is True

        # Stop runtime with an extremely short timeout (0.01s); worker is still blocked
        ctrl.runtime.stop(timeout=0.01)

        # Phase 7B safety invariant: TaskStore must NOT be closed while worker is still alive
        assert ctrl.runtime.scheduler.is_running is True
        assert getattr(ctrl.task_manager.store, "_closed", False) is False

    finally:
        # Always unblock the worker and complete clean shutdown so no thread leaks
        unblock_callback.set()
        ctrl.runtime.stop(timeout=2.0)
        assert ctrl.runtime.scheduler.is_running is False
        assert getattr(ctrl.task_manager.store, "_closed", False) is True


def test_controller_stop_worker_hang_defers_clean_stop(
    e2e_controller_factory: ControllerFactory,
    fake_clock: FakeClock,
) -> None:
    """Scenario 8: Controller stop() defers clean stop if scheduler worker is alive, completing on subsequent call."""
    entered_callback = threading.Event()
    unblock_callback = threading.Event()

    def _blocking_notification_handler(notif: ReminderNotification) -> None:
        entered_callback.set()
        unblock_callback.wait(timeout=3.0)

    # Disable missed reminder recovery on start so task is picked up by background scheduler worker
    config = AppConfig(
        task_recover_missed_reminders=False,
        task_scheduler_poll_interval=0.05,
    )

    ctrl = e2e_controller_factory(
        config=config,
        clock=fake_clock,
        custom_notification_handler=CallbackNotificationHandler(_blocking_notification_handler),
    )

    try:
        # Create a due task to trigger blocking notification in the background worker
        ctrl.task_manager.create_task(
            title="Controller Hang Lifecycle Task",
            reminder_at=fake_clock.now() - timedelta(minutes=1),
        )

        ctrl.start()

        # Wait until background worker enters the callback
        assert entered_callback.wait(timeout=2.0) is True

        # Call ctrl.stop() with tiny timeout so worker join times out
        ctrl.stop(timeout=0.01)

        # Invariant 1: Controller must NOT mark itself cleanly stopped while worker is active
        assert ctrl.is_cleanly_stopped is False
        assert ctrl._cleanly_stopped is False

        # Invariant 2: TaskStore remains open while worker thread is active
        assert ctrl.runtime.scheduler.is_running is True
        assert getattr(ctrl.task_manager.store, "_closed", False) is False

    finally:
        # Always unblock callback so worker can terminate safely, even if an assertion failed above
        unblock_callback.set()

        # Call ctrl.stop() again to complete shutdown
        ctrl.stop(timeout=2.0)

        # Invariant 3: Scheduler worker terminated
        assert ctrl.runtime.scheduler.is_running is False

        # Invariant 4: TaskStore is now safely closed
        assert getattr(ctrl.task_manager.store, "_closed", False) is True

        # Invariant 5: Controller is now cleanly stopped
        assert ctrl.is_cleanly_stopped is True
        assert ctrl._cleanly_stopped is True

        # Invariant 6: Repeated ctrl.stop() is safe and idempotent
        ctrl.stop(timeout=1.0)
        assert ctrl.is_cleanly_stopped is True
