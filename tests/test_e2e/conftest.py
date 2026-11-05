"""Shared fixtures and infrastructure helpers for Phase 8B E2E validation."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Generator

import pytest

from app.bootstrap import create_controller
from app.controller import AssistantController
from app.notifications.models import ReminderNotification


@dataclass(frozen=True)
class TempAnayaPaths:
    """Isolated filesystem paths for temporary E2E test databases."""

    data_dir: Path
    tasks_db: Path
    memory_db: Path

    def __getitem__(self, item: str) -> Path:
        """Allow dict-style key access for convenience."""
        return getattr(self, item)


class FakeClock:
    """Deterministic, thread-safe clock helper supporting datetime injection and advancement."""

    def __init__(self, initial_time: datetime | None = None) -> None:
        if initial_time is None:
            self._now = datetime(2026, 11, 5, 12, 0, 0, tzinfo=timezone.utc)
        elif initial_time.tzinfo is None:
            self._now = initial_time.replace(tzinfo=timezone.utc)
        else:
            self._now = initial_time.astimezone(timezone.utc)
        self._lock = threading.Lock()

    def __call__(self) -> datetime:
        with self._lock:
            return self._now

    def now(self) -> datetime:
        with self._lock:
            return self._now

    def advance(self, delta: timedelta | float | int) -> datetime:
        """Advance time by a timedelta or by numeric seconds."""
        if isinstance(delta, (int, float)):
            delta = timedelta(seconds=delta)
        with self._lock:
            self._now += delta
            return self._now

    def set(self, new_time: datetime) -> None:
        """Set the clock explicitly to a specific datetime."""
        if new_time.tzinfo is None:
            new_time = new_time.replace(tzinfo=timezone.utc)
        else:
            new_time = new_time.astimezone(timezone.utc)
        with self._lock:
            self._now = new_time


class RecordingNotificationHandler:
    """Thread-safe recording notification handler implementing the NotificationHandler protocol.

    Records received ReminderNotification objects and signals a threading.Event
    to enable deterministic synchronization with the background scheduler thread
    without fixed sleeps.
    """

    def __init__(self) -> None:
        self._notifications: list[ReminderNotification] = []
        self._lock = threading.Lock()
        self._event = threading.Event()

    @property
    def event(self) -> threading.Event:
        """Event signaled whenever a notification arrives."""
        return self._event

    @property
    def notifications(self) -> list[ReminderNotification]:
        """Snapshot of all received notifications."""
        with self._lock:
            return list(self._notifications)

    def notify(self, notification: ReminderNotification) -> None:
        """Receive and record a reminder notification, setting the synchronization event."""
        with self._lock:
            self._notifications.append(notification)
            self._event.set()

    def wait(self, timeout: float = 5.0) -> bool:
        """Wait for at least one notification to be received. Returns False on timeout."""
        return self._event.wait(timeout=timeout)

    def clear(self) -> None:
        """Clear recorded notifications and reset the synchronization event."""
        with self._lock:
            self._notifications.clear()
            self._event.clear()


@pytest.fixture
def temp_anaya_dir(tmp_path: Path) -> TempAnayaPaths:
    """Provide isolated directory and database file paths for E2E tests."""
    data_dir = tmp_path / "anaya_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return TempAnayaPaths(
        data_dir=data_dir,
        tasks_db=data_dir / "tasks.db",
        memory_db=data_dir / "memory.db",
    )


@pytest.fixture
def fake_clock() -> FakeClock:
    """Provide a deterministic FakeClock initialized to UTC."""
    return FakeClock()


@pytest.fixture
def recording_notifications() -> RecordingNotificationHandler:
    """Provide a fresh RecordingNotificationHandler instance."""
    return RecordingNotificationHandler()


ControllerFactory = Callable[..., AssistantController]


@pytest.fixture
def e2e_controller_factory(
    temp_anaya_dir: TempAnayaPaths,
) -> Generator[ControllerFactory, None, None]:
    """Factory fixture creating AssistantController instances pointing to isolated temporary databases.

    Defaults:
        - enable_speech=False (text-only, zero audio hardware dependencies)
        - enable_ai=False (zero local model dependencies or network calls)
        - tasks_db_path=temp_anaya_dir.tasks_db
        - memory_db_path=temp_anaya_dir.memory_db

    Guarantees:
        - Tracks every constructed controller.
        - Supports creating sequential controllers using identical database paths (for persistence testing).
        - Safe teardown in reverse construction order:
            * Cleans up unstarted, running, and partially stopped controllers.
            * Bounded shutdown timeout (2.0 seconds).
            * Preserves TaskStore if scheduler thread has not exited (Phase 7B safety invariant).
            * Aggregates and reports cleanup failures instead of silently swallowing them.
    """
    constructed_controllers: list[AssistantController] = []

    def _create(**overrides: Any) -> AssistantController:
        default_kwargs: dict[str, Any] = {
            "tasks_db_path": temp_anaya_dir.tasks_db,
            "memory_db_path": temp_anaya_dir.memory_db,
            "enable_speech": False,
            "enable_ai": False,
        }
        default_kwargs.update(overrides)
        ctrl = create_controller(**default_kwargs)
        constructed_controllers.append(ctrl)
        return ctrl

    yield _create

    # Teardown: attempt cleanup for every constructed controller in reverse order
    cleanup_errors: list[tuple[AssistantController, Exception]] = []
    for ctrl in reversed(constructed_controllers):
        try:
            ctrl.stop(timeout=2.0)
        except Exception as exc:
            cleanup_errors.append((ctrl, exc))

    if cleanup_errors:
        error_messages = [
            f"Controller {id(c)} ({type(e).__name__}): {e}"
            for c, e in cleanup_errors
        ]
        pytest.fail(
            f"E2E fixture teardown encountered {len(cleanup_errors)} cleanup failure(s):\n"
            + "\n".join(error_messages)
        )
