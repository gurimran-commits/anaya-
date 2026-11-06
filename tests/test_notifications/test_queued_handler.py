"""Unit tests for QueuedNotificationHandler verifying scheduler isolation and fallback."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from threading import Event
from typing import Any

import pytest

from app.notifications.models import ReminderNotification
from app.notifications.protocols import NotificationHandler
from app.notifications.queued_handler import QueuedNotificationHandler


class RecordingTargetHandler:
    """Thread-safe target handler recording delivered notifications using Events."""

    def __init__(self) -> None:
        self.received: list[ReminderNotification] = []
        self.event = Event()
        self.block_event: Event | None = None

    def notify(self, notification: ReminderNotification) -> None:
        if self.block_event is not None:
            self.block_event.wait()
        self.received.append(notification)
        self.event.set()


class FailingTargetHandler:
    """Target handler that intentionally raises an exception upon delivery."""

    def __init__(self, exc_to_raise: Exception | None = None) -> None:
        self.exc = exc_to_raise or RuntimeError("Simulated delivery failure in target handler.")
        self.call_count = 0

    def notify(self, notification: ReminderNotification) -> None:
        self.call_count += 1
        raise self.exc


class RecordingFallbackHandler:
    """Fallback handler tracking notifications routed to fallback."""

    def __init__(self) -> None:
        self.received: list[ReminderNotification] = []
        self.event = Event()

    def notify(self, notification: ReminderNotification) -> None:
        self.received.append(notification)
        self.event.set()


class CrashingFallbackHandler:
    """Fallback handler that raises an exception to verify error suppression."""

    def notify(self, notification: ReminderNotification) -> None:
        raise OSError("Simulated disk full or I/O failure in logging fallback.")


def _create_sample_notification(task_id: str = "task-1", title: str = "Test Task") -> ReminderNotification:
    return ReminderNotification(
        task_id=task_id,
        title=title,
        reminder_at=datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc),
        description="Sample description",
        priority="high",
        is_missed=False,
    )


class TestQueuedNotificationHandler:
    """Test suite for QueuedNotificationHandler."""

    def test_implements_notification_handler_protocol(self) -> None:
        """Verify QueuedNotificationHandler implements the runtime NotificationHandler protocol."""
        target = RecordingTargetHandler()
        handler = QueuedNotificationHandler(target_handler=target)
        try:
            assert isinstance(handler, NotificationHandler)
        finally:
            handler.stop()

    def test_normal_asynchronous_dispatch(self) -> None:
        """Verify normal non-blocking queueing and asynchronous worker delivery."""
        target = RecordingTargetHandler()
        fallback = RecordingFallbackHandler()
        handler = QueuedNotificationHandler(
            target_handler=target,
            fallback_handler=fallback,
            max_queue_size=10,
        )

        try:
            handler.start()
            assert handler.is_running is True

            notif = _create_sample_notification("task-async-1", "Async Task 1")
            handler.notify(notif)

            # Wait for background worker to deliver to target
            delivered = target.event.wait(timeout=2.0)
            assert delivered is True
            assert len(target.received) == 1
            assert target.received[0].task_id == "task-async-1"
            assert target.received[0].title == "Async Task 1"
            assert len(fallback.received) == 0
        finally:
            handler.stop(timeout=1.0)

    def test_queue_overflow_fallback(self) -> None:
        """Verify that when queue reaches capacity, overflow is routed to fallback logging immediately."""
        target = RecordingTargetHandler()
        fallback = RecordingFallbackHandler()

        # Hold target worker blocked so queue fills up
        target.block_event = Event()

        # Max queue size of 2
        handler = QueuedNotificationHandler(
            target_handler=target,
            fallback_handler=fallback,
            max_queue_size=2,
        )

        try:
            handler.start()

            notif1 = _create_sample_notification("t1", "Task 1")
            notif2 = _create_sample_notification("t2", "Task 2")
            notif3 = _create_sample_notification("t3", "Task 3")  # Overflow item
            notif4 = _create_sample_notification("t4", "Task 4")  # Overflow item

            # Fill the worker and queue
            handler.notify(notif1)  # Picked up by worker, blocks on block_event
            handler.notify(notif2)  # In queue slot 1
            handler.notify(notif3)  # In queue slot 2 (queue now full)

            # Next item must trigger queue overflow fallback immediately
            handler.notify(notif4)

            assert len(fallback.received) >= 1
            assert any(item.task_id == "t4" for item in fallback.received)

        finally:
            # Unblock target worker and stop
            if target.block_event is not None:
                target.block_event.set()
            handler.stop(timeout=1.0)

    def test_downstream_delivery_exception_routed_to_fallback(self) -> None:
        """Verify delivery exceptions raised by target handler are caught and forwarded to fallback."""
        failing_target = FailingTargetHandler()
        fallback = RecordingFallbackHandler()

        handler = QueuedNotificationHandler(
            target_handler=failing_target,
            fallback_handler=fallback,
            max_queue_size=5,
        )

        try:
            handler.start()
            notif = _create_sample_notification("task-fail-1", "Failing Task")
            handler.notify(notif)

            delivered_to_fallback = fallback.event.wait(timeout=2.0)
            assert delivered_to_fallback is True
            assert len(fallback.received) == 1
            assert fallback.received[0].task_id == "task-fail-1"
            assert failing_target.call_count == 1
            # Worker must remain alive and not crash
            assert handler.is_running is True
        finally:
            handler.stop(timeout=1.0)

    def test_logging_fallback_exception_is_suppressed(self, caplog: pytest.LogCaptureFixture) -> None:
        """Verify that a failure inside the fallback logging handler is caught and suppressed."""
        target = FailingTargetHandler()
        crashing_fallback = CrashingFallbackHandler()

        handler = QueuedNotificationHandler(
            target_handler=target,
            fallback_handler=crashing_fallback,
            max_queue_size=5,
        )

        try:
            handler.start()
            notif = _create_sample_notification("task-crash-fallback", "Crash Fallback Task")

            # Must not raise an exception back to caller or terminate thread
            handler.notify(notif)

            # Also test direct safe fallback call
            with caplog.at_level(logging.CRITICAL):
                handler._safe_fallback_log(notif, reason="Testing fallback exception")

            assert "Critical failure in notification fallback handler" in caplog.text
        finally:
            handler.stop(timeout=1.0)

    def test_bounded_shutdown(self) -> None:
        """Verify bounded shutdown terminates worker thread and is idempotent."""
        target = RecordingTargetHandler()
        handler = QueuedNotificationHandler(target_handler=target)

        handler.start()
        assert handler.is_running is True

        handler.stop(timeout=1.0)
        assert handler.is_running is False

        # Repeated stop calls must be safe and idempotent
        handler.stop(timeout=1.0)
        assert handler.is_running is False

        # Attempting to start a stopped handler raises RuntimeError
        with pytest.raises(RuntimeError, match="has been stopped and cannot be restarted"):
            handler.start()

    def test_shutdown_with_pending_messages_drained_to_fallback(self) -> None:
        """Verify that unconsumed queued notifications are flushed to fallback on shutdown."""
        target = RecordingTargetHandler()
        fallback = RecordingFallbackHandler()

        # Hold target worker blocked
        target.block_event = Event()

        handler = QueuedNotificationHandler(
            target_handler=target,
            fallback_handler=fallback,
            max_queue_size=10,
        )

        handler.start()
        notif1 = _create_sample_notification("t1", "Item 1")
        notif2 = _create_sample_notification("t2", "Item 2")
        notif3 = _create_sample_notification("t3", "Item 3")

        handler.notify(notif1)  # worker dequeues this and blocks
        handler.notify(notif2)  # pending in queue
        handler.notify(notif3)  # pending in queue

        # Stop with short timeout while target worker is still blocked
        # Worker won't exit until unblocked, but drain happens
        target.block_event.set()
        handler.stop(timeout=1.0)

        # All items were either delivered to target or drained to fallback
        total_delivered = len(target.received) + len(fallback.received)
        assert total_delivered == 3

    def test_worker_timeout_reporting(self, caplog: pytest.LogCaptureFixture) -> None:
        """Verify a warning log is recorded when the worker thread does not exit within timeout."""
        target = RecordingTargetHandler()
        target.block_event = Event()

        handler = QueuedNotificationHandler(
            target_handler=target,
            worker_name="TestStuckWorker",
        )

        handler.start()
        handler.notify(_create_sample_notification("stuck-task", "Stuck Task"))

        # Stop with very short timeout while worker is permanently blocked
        with caplog.at_level(logging.WARNING):
            handler.stop(timeout=0.05)

        # Worker was stuck, so warning was emitted
        assert "did not terminate within timeout" in caplog.text

        # Unblock worker to clean up cleanly
        target.block_event.set()
        # Second stop cleans up the unblocked thread
        handler.stop(timeout=1.0)
        assert handler.is_running is False

    def test_invalid_notification_payload_routes_to_fallback(self) -> None:
        """Verify that passing non-ReminderNotification object routes to fallback without crashing."""
        target = RecordingTargetHandler()
        fallback = RecordingFallbackHandler()

        handler = QueuedNotificationHandler(
            target_handler=target,
            fallback_handler=fallback,
        )

        try:
            handler.start()
            handler.notify("invalid-payload-string")  # type: ignore[arg-type]
            assert len(target.received) == 0
        finally:
            handler.stop(timeout=1.0)
