"""Tests for notification payload structure, handlers, and failure isolation."""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.notifications.handlers import (
    CallbackNotificationHandler,
    CompositeNotificationHandler,
    LoggingNotificationHandler,
    SpeechNotificationHandler,
)
from app.notifications.models import ReminderNotification
from app.runtime import AnayaRuntime
from app.tasks.models import Task


@pytest.fixture
def temp_db(tmp_path):
    return tmp_path / "test_notifications.db"


def test_reminder_notification_payload_formatting():
    """Verify ReminderNotification formats messages clearly for normal and missed reminders."""
    now = datetime(2026, 11, 5, 14, 0, tzinfo=timezone.utc)
    task = Task(
        id="a1b2c3d4",
        title="Submit research paper",
        description="Include bibliography in PDF",
        reminder_at=now,
        priority="high",
    )

    notif_normal = ReminderNotification.from_task(task, is_missed=False)
    assert notif_normal.task_id == "a1b2c3d4"
    assert notif_normal.title == "Submit research paper"
    assert notif_normal.is_missed is False
    assert "Reminder: 'Submit research paper' is due now" in notif_normal.message
    assert "Note: Include bibliography in PDF" in notif_normal.message

    notif_missed = ReminderNotification.from_task(task, is_missed=True)
    assert notif_missed.is_missed is True
    assert "Missed reminder for task 'Submit research paper'" in notif_missed.message


def test_callback_receives_correct_notification_payload(temp_db):
    """Verify runtime delivers accurate payload to on_reminder callback."""
    received: list[ReminderNotification] = []

    now = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
    clock = lambda: now

    runtime = AnayaRuntime(
        db_path=temp_db,
        on_reminder=lambda notif: received.append(notif),
        clock=clock,
    )
    try:
        # Create a task due at 12:00
        task = runtime.task_manager.create_task(
            title="Dentist appointment",
            description="Bring dental card",
            reminder_at=now,
        )

        # Synchronously tick scheduler
        dispatched = runtime.run_scheduler_pending(now=now)
        assert dispatched == 1
        assert len(received) == 1
        assert received[0].task_id == task.id
        assert received[0].title == "Dentist appointment"
        assert received[0].description == "Bring dental card"
        assert received[0].is_missed is False
    finally:
        runtime.stop()


def test_multiple_simultaneous_reminders(temp_db):
    """Verify runtime dispatches multiple reminders due at the exact same instant."""
    received: list[ReminderNotification] = []
    now = datetime(2026, 11, 5, 15, 30, tzinfo=timezone.utc)

    runtime = AnayaRuntime(
        db_path=temp_db,
        on_reminder=lambda notif: received.append(notif),
        clock=lambda: now,
    )
    try:
        t1 = runtime.task_manager.create_task("Task Alpha", reminder_at=now)
        t2 = runtime.task_manager.create_task("Task Beta", reminder_at=now)
        t3 = runtime.task_manager.create_task("Task Gamma", reminder_at=now)

        dispatched = runtime.run_scheduler_pending(now=now)
        assert dispatched == 3
        assert len(received) == 3
        received_ids = {n.task_id for n in received}
        assert received_ids == {t1.id, t2.id, t3.id}
    finally:
        runtime.stop()


def test_callback_failure_is_isolated(temp_db):
    """If notification callback raises an exception, scheduler continues without crashing."""
    attempted: list[str] = []

    def unstable_callback(notif: ReminderNotification):
        attempted.append(notif.task_id)
        if notif.title == "Failing Task":
            raise RuntimeError("UI notification display failed")

    now = datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc)
    runtime = AnayaRuntime(
        db_path=temp_db,
        on_reminder=unstable_callback,
        clock=lambda: now,
    )
    try:
        t1 = runtime.task_manager.create_task("Failing Task", reminder_at=now)
        t2 = runtime.task_manager.create_task("Succeeding Task", reminder_at=now)

        # Scheduler must process both tasks despite t1 raising an error in the callback
        dispatched = runtime.run_scheduler_pending(now=now)
        assert len(attempted) == 2
        assert t1.id in attempted
        assert t2.id in attempted
    finally:
        runtime.stop()


def test_composite_notification_handler():
    """Verify CompositeNotificationHandler fans out to all registered handlers."""
    calls_a: list[ReminderNotification] = []
    calls_b: list[ReminderNotification] = []

    handler = CompositeNotificationHandler(
        [
            CallbackNotificationHandler(lambda n: calls_a.append(n)),
            CallbackNotificationHandler(lambda n: calls_b.append(n)),
        ]
    )

    now = datetime.now(timezone.utc)
    notif = ReminderNotification(task_id="xyz", title="Test", reminder_at=now)
    handler.notify(notif)

    assert len(calls_a) == 1
    assert len(calls_b) == 1


def test_speech_notification_handler_delegation():
    """Verify SpeechNotificationHandler invokes TTS engine without microphone activation."""
    spoken: list[str] = []

    class MockTTSEngine:
        def speak(self, text: str) -> None:
            spoken.append(text)

    handler = SpeechNotificationHandler(tts_engine=MockTTSEngine())
    now = datetime.now(timezone.utc)
    notif = ReminderNotification(task_id="123", title="Check oven", reminder_at=now)
    handler.notify(notif)

    assert len(spoken) == 1
    assert "Check oven" in spoken[0]
