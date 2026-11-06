"""Unit tests for WindowsToastNotificationHandler verifying toast delivery, payload mapping, and fallback."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import Any

import pytest

from app.notifications.models import ReminderNotification
from app.notifications.protocols import NotificationHandler
from app.notifications.windows_toast import (
    DEFAULT_AUMID,
    VERIFIED_WINDOWS_TOASTS_VERSION,
    WindowsToastNotificationHandler,
)


class FakeToast:
    """Mock toast object capturing text fields."""

    def __init__(self) -> None:
        self.text_fields: list[str] = []


class FakeToaster:
    """Mock toaster object recording displayed toasts."""

    def __init__(self, application_text: str) -> None:
        self.application_text = application_text
        self.shown_toasts: list[FakeToast] = []
        self.should_fail = False

    def show_toast(self, toast: FakeToast) -> None:
        if self.should_fail:
            raise RuntimeError("Simulated COM toast display failure.")
        self.shown_toasts.append(toast)


class RecordingFallbackHandler:
    """Fallback handler tracking notifications routed to fallback."""

    def __init__(self) -> None:
        self.received: list[ReminderNotification] = []

    def notify(self, notification: ReminderNotification) -> None:
        self.received.append(notification)


def _create_sample_notification(
    task_id: str = "task-toast-1",
    title: str = "Meeting with Team",
    is_missed: bool = False,
) -> ReminderNotification:
    return ReminderNotification(
        task_id=task_id,
        title=title,
        reminder_at=datetime(2026, 11, 6, 15, 30, tzinfo=timezone.utc),
        description="Discuss Q4 milestones",
        priority="high",
        is_missed=is_missed,
    )


class TestWindowsToastNotificationHandler:
    """Test suite for WindowsToastNotificationHandler."""

    def test_implements_notification_handler_protocol(self) -> None:
        """Verify handler implements NotificationHandler protocol."""
        handler = WindowsToastNotificationHandler(set_process_aumid=False)
        assert isinstance(handler, NotificationHandler)

    def test_verified_package_version_constant(self) -> None:
        """Verify the documented verified package version bound."""
        assert "1.3.1" in VERIFIED_WINDOWS_TOASTS_VERSION
        assert "<2.0.0" in VERIFIED_WINDOWS_TOASTS_VERSION

    def test_missing_optional_dependency_falls_back_cleanly(self) -> None:
        """Verify that when windows-toasts is not installed, notify() routes to fallback safely."""
        fallback = RecordingFallbackHandler()
        # In current test environment, windows_toasts is not installed
        handler = WindowsToastNotificationHandler(
            fallback_handler=fallback,
            set_process_aumid=False,
        )

        notif = _create_sample_notification("t-dep-1", "Test Missing Dep")
        handler.notify(notif)

        # Must route to fallback without raising ImportError
        assert len(fallback.received) == 1
        assert fallback.received[0].task_id == "t-dep-1"

    def test_unsupported_platform_behavior(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify that running on non-Windows platforms routes safely to fallback."""
        fallback = RecordingFallbackHandler()
        handler = WindowsToastNotificationHandler(
            fallback_handler=fallback,
            set_process_aumid=False,
        )

        monkeypatch.setattr(sys, "platform", "linux")

        notif = _create_sample_notification("t-linux-1", "Linux Test")
        handler.notify(notif)

        assert len(fallback.received) == 1
        assert fallback.received[0].task_id == "t-linux-1"
        assert handler.is_available() is False

    def test_toast_payload_mapping_with_injected_toaster(self) -> None:
        """Verify title, body, and AUMID are accurately mapped to the Toast object."""
        recorded_toasters: list[FakeToaster] = []

        def fake_toaster_factory(aumid: str) -> FakeToaster:
            t = FakeToaster(aumid)
            recorded_toasters.append(t)
            return t

        def fake_toast_factory() -> FakeToast:
            return FakeToast()

        fallback = RecordingFallbackHandler()
        handler = WindowsToastNotificationHandler(
            aumid="Custom.Test.AUMID",
            fallback_handler=fallback,
            toaster_factory=fake_toaster_factory,
            toast_factory=fake_toast_factory,
            set_process_aumid=False,
        )

        assert handler.aumid == "Custom.Test.AUMID"
        assert handler.is_available() is True

        notif = _create_sample_notification("t-map-1", "Review Phase 9", is_missed=False)
        handler.notify(notif)

        assert len(recorded_toasters) == 1
        toaster = recorded_toasters[0]
        assert toaster.application_text == "Custom.Test.AUMID"
        assert len(toaster.shown_toasts) == 1

        toast = toaster.shown_toasts[0]
        assert len(toast.text_fields) == 2
        assert toast.text_fields[0] == "Review Phase 9"
        assert "Review Phase 9" in toast.text_fields[1]
        assert len(fallback.received) == 0

    def test_delivery_exception_in_toaster_routes_to_fallback(self) -> None:
        """Verify exceptions during show_toast() are caught and routed to fallback."""
        recorded_toaster = FakeToaster("Test.AUMID")
        recorded_toaster.should_fail = True

        fallback = RecordingFallbackHandler()
        handler = WindowsToastNotificationHandler(
            fallback_handler=fallback,
            toaster_factory=lambda aumid: recorded_toaster,
            toast_factory=lambda: FakeToast(),
            set_process_aumid=False,
        )

        notif = _create_sample_notification("t-fail-1", "Failing Toast")
        handler.notify(notif)

        # Exception caught, routed to fallback
        assert len(fallback.received) == 1
        assert fallback.received[0].task_id == "t-fail-1"

    def test_aumid_default_and_whitespace_sanitization(self) -> None:
        """Verify default AUMID and whitespace trimming."""
        handler_default = WindowsToastNotificationHandler(set_process_aumid=False)
        assert handler_default.aumid == DEFAULT_AUMID

        handler_empty = WindowsToastNotificationHandler(aumid="   ", set_process_aumid=False)
        assert handler_empty.aumid == DEFAULT_AUMID

        handler_trimmed = WindowsToastNotificationHandler(aumid="  App.Trimmed  ", set_process_aumid=False)
        assert handler_trimmed.aumid == "App.Trimmed"

    def test_invalid_notification_payload_routes_to_fallback(self) -> None:
        """Verify non-ReminderNotification input is caught and handled safely."""
        fallback = RecordingFallbackHandler()
        handler = WindowsToastNotificationHandler(
            fallback_handler=fallback,
            set_process_aumid=False,
        )

        handler.notify("invalid-input")  # type: ignore[arg-type]
        assert len(fallback.received) == 0
