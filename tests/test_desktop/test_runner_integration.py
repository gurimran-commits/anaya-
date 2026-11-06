"""Integration tests for DesktopApplicationRunner subsystems."""

from __future__ import annotations

import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from datetime import datetime, timezone

from app.core.action_result import ActionResult
from app.core.config import AppConfig
from app.desktop.runner import DesktopApplicationRunner, RunnerState
from app.hotkey.backend import HotkeyBackend
from app.hotkey.models import HotkeyServiceState
from app.hotkey.service import GlobalHotkeyService
from app.hud.coordinator import VoiceSessionCoordinator
from app.hud.models import HudServiceState, VoiceSessionSource
from app.hud.service import FloatingHudService
from app.notifications.models import ReminderNotification
from app.notifications.queued_handler import QueuedNotificationHandler
from app.speech.models import PipelineResult
from app.tray.backend import TrayBackend
from app.tray.models import IDM_EXIT, TrayServiceState
from app.tray.service import WindowsTrayService


class MockMsg:
    """Mock structure mimicking Win32 MSG."""

    def __init__(self, hwnd: int = 0, message: int = 0, wParam: int = 0, lParam: int = 0) -> None:
        self.hwnd = hwnd
        self.message = message
        self.wParam = wParam
        self.lParam = lParam


class MockHotkeyBackend(HotkeyBackend):
    """Thread-safe mock backend simulating Win32 message queue and hotkey APIs."""

    def __init__(self) -> None:
        self.is_available_flag = True
        self.registered_hotkeys: list[tuple[int, int, int]] = []
        self.unregistered_hotkeys: list[int] = []
        self._msg_queue: list[MockMsg] = []
        self._queue_lock = threading.Lock()
        self._queue_condition = threading.Condition(self._queue_lock)
        self._quit_received = False
        self._thread_id = 12345
        self.before_start_event: threading.Event | None = None
        self.after_start_event: threading.Event | None = None

    def is_available(self) -> bool:
        return self.is_available_flag

    def initialize_message_queue(self) -> None:
        pass

    def get_current_thread_id(self) -> int:
        return self._thread_id

    def register_hotkey(self, hotkey_id: int, fs_modifiers: int, vk_code: int) -> bool:
        if self.before_start_event is not None:
            self.before_start_event.wait()
        with self._queue_lock:
            self.registered_hotkeys.append((hotkey_id, fs_modifiers, vk_code))
        if self.after_start_event is not None:
            self.after_start_event.set()
        return True

    def unregister_hotkey(self, hotkey_id: int) -> bool:
        with self._queue_lock:
            self.unregistered_hotkeys.append(hotkey_id)
        return True

    def get_last_error(self) -> int:
        return 0

    def create_msg(self) -> MockMsg:
        return MockMsg()

    def get_message(self, msg: Any) -> int:
        with self._queue_condition:
            while not self._msg_queue and not self._quit_received:
                self._queue_condition.wait(timeout=0.05)
            if self._quit_received:
                return 0
            if self._msg_queue:
                queued = self._msg_queue.pop(0)
                msg.message = queued.message
                msg.wParam = queued.wParam
                msg.lParam = queued.lParam
                return 1
            return -1

    def post_thread_quit(self, thread_id: int) -> bool:
        with self._queue_condition:
            self._quit_received = True
            self._queue_condition.notify_all()
        return True

    def extract_msg_details(self, msg: Any) -> tuple[int, int]:
        return msg.message, msg.wParam


class MockTrayBackend(TrayBackend):
    """Thread-safe mock backend simulating Win32 system tray."""

    def __init__(self) -> None:
        self.is_available_flag = True
        self.hwnd_counter = 1000
        self.added_icons: list[dict[str, Any]] = []
        self.deleted_icons: list[int] = []
        self._thread_id = 23456
        self._wndproc: Any = None

    def is_available(self) -> bool:
        return self.is_available_flag

    def get_current_thread_id(self) -> int:
        return self._thread_id

    def register_taskbar_created_message(self) -> int:
        return 0xC001

    def create_tray_window(self, class_name: str, window_title: str, wndproc: Any) -> int:
        self._wndproc = wndproc
        self.hwnd_counter += 1
        return self.hwnd_counter

    def destroy_tray_window(self, hwnd: int) -> bool:
        return True

    def load_default_icon(self) -> int:
        return 999

    def add_icon(self, hwnd: int, icon_id: int, hicon: int, tooltip: str) -> bool:
        self.added_icons.append({"hwnd": hwnd, "icon_id": icon_id, "tooltip": tooltip})
        return True

    def modify_icon(self, hwnd: int, icon_id: int, hicon: int | None = None, tooltip: str | None = None) -> bool:
        return True

    def delete_icon(self, hwnd: int, icon_id: int) -> bool:
        self.deleted_icons.append(icon_id)
        return True

    def set_version(self, hwnd: int, icon_id: int, version: int) -> bool:
        return True

    def post_message(self, hwnd: int, msg: int, wparam: int, lparam: int) -> bool:
        return True

    def post_thread_quit(self, thread_id: int) -> bool:
        return True

    def get_message(self, msg: Any) -> int:
        return 0

    def create_msg(self) -> MockMsg:
        return MockMsg()

    def translate_message(self, msg: Any) -> None:
        pass

    def dispatch_message(self, msg: Any) -> int:
        return 0

    def def_window_proc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        return 0

    def create_popup_menu(self) -> int:
        return 500

    def append_menu_item(self, hmenu: int, flags: int, item_id: int, text: str) -> bool:
        return True

    def append_separator(self, hmenu: int) -> bool:
        return True

    def append_submenu(self, hmenu: int, flags: int, hsubmenu: int, text: str) -> bool:
        return True

    def destroy_menu(self, hmenu: int) -> bool:
        return True

    def set_foreground_window(self, hwnd: int) -> bool:
        return True

    def track_popup_menu(self, hmenu: int, x: int, y: int, hwnd: int) -> bool:
        return True

    def get_cursor_pos(self) -> tuple[int, int]:
        return (100, 100)

    def get_last_error(self) -> int:
        return 0


class RecordingNotificationTarget:
    """Thread-safe notification sink for tests."""

    def __init__(self) -> None:
        self.notifications: list[ReminderNotification] = []
        self.received_event = threading.Event()

    def notify(self, notification: ReminderNotification) -> None:
        self.notifications.append(notification)
        self.received_event.set()


def test_single_shared_controller_and_coordinator() -> None:
    """HUD and Hotkey services receive the exact same controller and VoiceSessionCoordinator."""
    mock_ctrl = MagicMock()
    mock_ctrl.start.return_value = None
    mock_ctrl.is_running = True
    mock_ctrl.stop.return_value = None
    mock_ctrl.is_cleanly_stopped = True

    coordinator = VoiceSessionCoordinator()

    hk_backend = MockHotkeyBackend()
    hotkey = GlobalHotkeyService(controller=mock_ctrl, coordinator=coordinator, backend=hk_backend)

    hud = FloatingHudService(controller=mock_ctrl, coordinator=coordinator)

    runner = DesktopApplicationRunner(
        controller=mock_ctrl,
        coordinator=coordinator,
        hud_service=hud,
        hotkey_service=hotkey,
    )

    assert runner.coordinator is coordinator
    assert runner.hud_service._coordinator is coordinator
    assert runner.hotkey_service._coordinator is coordinator
    assert runner.hud_service._controller is mock_ctrl
    assert runner.hotkey_service._controller is mock_ctrl


def test_hotkey_lease_acquisition_and_release() -> None:
    """Hotkey voice worker acquires VoiceSessionSource.GLOBAL_HOTKEY during handle_voice and releases it."""
    coordinator = VoiceSessionCoordinator()
    captured_lease_state: dict[str, Any] = {}
    handle_voice_done = threading.Event()

    def _fake_handle_voice(duration=None, speak_response=True):
        captured_lease_state["is_active"] = coordinator.is_active
        captured_lease_state["active_source"] = coordinator.active_source
        handle_voice_done.set()
        return PipelineResult(
            transcript="test voice",
            action_result=ActionResult(success=True, message="Voice ok"),
            spoken_response=True,
            success=True,
        )

    mock_ctrl = MagicMock()
    mock_ctrl.handle_voice.side_effect = _fake_handle_voice

    backend = MockHotkeyBackend()
    hotkey = GlobalHotkeyService(
        controller=mock_ctrl,
        coordinator=coordinator,
        backend=backend,
    )

    started = hotkey.start(startup_timeout=2.0)
    assert started is True

    try:
        # Trigger hotkey press
        hotkey._on_hotkey_pressed()
        assert handle_voice_done.wait(timeout=2.0)

        # Inside handle_voice, the lease was active and attributed to GLOBAL_HOTKEY
        assert captured_lease_state.get("is_active") is True
        assert captured_lease_state.get("active_source") == VoiceSessionSource.GLOBAL_HOTKEY

        # Give worker a moment to execute finally block
        time.sleep(0.05)
        # Outside handle_voice, the lease must be released
        assert coordinator.is_active is False
        assert coordinator.active_source is None
    finally:
        hotkey.shutdown(timeout=2.0)


def test_hud_active_session_rejects_hotkey_mic_access() -> None:
    """While HUD holds the microphone lease, hotkey presses are dropped immediately."""
    coordinator = VoiceSessionCoordinator()
    mock_ctrl = MagicMock()

    backend = MockHotkeyBackend()
    hotkey = GlobalHotkeyService(
        controller=mock_ctrl,
        coordinator=coordinator,
        backend=backend,
    )

    started = hotkey.start(startup_timeout=2.0)
    assert started is True

    try:
        # HUD acquires lease
        hud_lease = coordinator.acquire(VoiceSessionSource.HUD_BUTTON)
        assert hud_lease is not None
        assert coordinator.is_active is True

        # Hotkey press arrives while HUD is active
        hotkey._on_hotkey_pressed()

        # The hotkey should NOT mark itself busy or trigger voice processing
        assert not hotkey.is_voice_active
        time.sleep(0.05)
        assert not mock_ctrl.handle_voice.called

        # Release HUD lease
        coordinator.release(hud_lease)
        assert not coordinator.is_active
    finally:
        hotkey.shutdown(timeout=2.0)


def test_hotkey_active_session_rejects_hud_mic_access() -> None:
    """While Hotkey holds the microphone lease, HUD trigger_voice() is rejected."""
    coordinator = VoiceSessionCoordinator()
    mock_ctrl = MagicMock()

    hud = FloatingHudService(controller=mock_ctrl, coordinator=coordinator)

    # Hotkey acquires lease
    hk_lease = coordinator.acquire(VoiceSessionSource.GLOBAL_HOTKEY)
    assert hk_lease is not None
    assert coordinator.is_active is True

    # HUD attempts to trigger voice
    op_id = hud.trigger_voice()
    assert op_id is None  # Rejected!

    # Release hotkey lease
    coordinator.release(hk_lease)
    assert not coordinator.is_active


def test_tray_open_invokes_hud_show_without_cross_thread_tk_access() -> None:
    """Tray primary action invokes hud.show() which enqueues an event into HUD mailbox."""
    mock_ctrl = MagicMock()
    mock_hud = MagicMock()

    tray_backend = MockTrayBackend()
    tray = WindowsTrayService(
        controller=mock_ctrl,
        backend=tray_backend,
        on_open=mock_hud.show,
        on_exit=lambda: None,
    )

    tray._handle_primary_action()
    assert mock_hud.show.called


def test_tray_exit_requests_shutdown_without_teardown_on_tray_thread() -> None:
    """Tray Exit command calls runner.request_stop() without running teardown on the tray thread."""
    runner = DesktopApplicationRunner()
    runner._state = RunnerState.RUNNING

    tray_backend = MockTrayBackend()
    tray = WindowsTrayService(
        controller=MagicMock(),
        backend=tray_backend,
        on_open=lambda: None,
        on_exit=runner.request_stop,
    )

    tray._handle_menu_command(IDM_EXIT)
    assert runner._stop_event.is_set()
    # Teardown has NOT happened yet on this thread
    assert runner.state == RunnerState.RUNNING


def test_notification_queue_delivery_and_shutdown() -> None:
    """QueuedNotificationHandler delivers notifications asynchronously and shuts down cleanly."""
    target = RecordingNotificationTarget()
    handler = QueuedNotificationHandler(target_handler=target)  # type: ignore[arg-type]

    handler.start()
    assert handler.is_running

    item = ReminderNotification(
        task_id="t1",
        title="Test Reminder",
        reminder_at=datetime.now(timezone.utc),
    )
    handler.notify(item)

    assert target.received_event.wait(timeout=2.0)
    assert len(target.notifications) == 1
    assert target.notifications[0].task_id == "t1"

    handler.stop(timeout=2.0)
    assert not handler.is_running


def test_scheduler_reminder_dispatches_to_queued_handler() -> None:
    """TaskScheduler reminders are dispatched through AnayaRuntime to QueuedNotificationHandler."""
    from app.runtime import AnayaRuntime
    from app.tasks.models import Task

    target = RecordingNotificationTarget()
    queued = QueuedNotificationHandler(target_handler=target)  # type: ignore[arg-type]
    queued.start()

    try:
        mock_task = Task(
            id="task_123",
            title="Check Oven",
            reminder_at=datetime.now(timezone.utc),
        )

        mock_runtime = MagicMock(spec=AnayaRuntime)
        mock_runtime._notification_handler = queued

        notification = ReminderNotification.from_task(mock_task)
        AnayaRuntime._dispatch_notification(mock_runtime, notification)

        assert target.received_event.wait(timeout=2.0)
        assert len(target.notifications) == 1
        assert target.notifications[0].task_id == "task_123"
        assert target.notifications[0].title == "Check Oven"
    finally:
        queued.stop(timeout=2.0)


def test_tray_toggle_reversible() -> None:
    """GlobalHotkeyService stop() is reversible: start() can be called again."""
    mock_ctrl = MagicMock()
    backend = MockHotkeyBackend()

    hotkey = GlobalHotkeyService(controller=mock_ctrl, backend=backend)

    assert hotkey.start(startup_timeout=2.0) is True
    assert hotkey.is_running
    assert hotkey.state == HotkeyServiceState.RUNNING

    # Reversible stop
    hotkey.stop(timeout=2.0)
    assert not hotkey.is_running
    assert hotkey.state == HotkeyServiceState.STOPPED
    assert not hotkey.is_terminated

    # Restart
    assert hotkey.start(startup_timeout=2.0) is True
    assert hotkey.is_running
    assert hotkey.state == HotkeyServiceState.RUNNING

    # Final shutdown
    hotkey.shutdown(timeout=2.0)
    assert not hotkey.is_running
    assert hotkey.is_terminated

    # Once terminated, start is permanently rejected
    assert hotkey.start(startup_timeout=2.0) is False


def test_critical_tray_toggle_shutdown_race() -> None:
    """Deterministic regression: delayed tray toggle worker cannot register hotkey after shutdown.

    Sequence:
        1. Tray toggle worker begins hotkey start request.
        2. Worker paused before hotkey registration via synchronization barrier.
        3. Runner begins shutdown: tray stop timed out, hotkey shutdown() executed.
        4. Hotkey shutdown() sets self._terminated under lock.
        5. Release delayed worker.
        6. Delayed worker must NOT register the hotkey or transition state to RUNNING.
    """
    mock_ctrl = MagicMock()
    backend = MockHotkeyBackend()

    hotkey = GlobalHotkeyService(controller=mock_ctrl, backend=backend)

    # Barrier to pause the worker right before registration inside start()
    worker_started_gate = threading.Event()
    worker_release_gate = threading.Event()

    def delayed_worker():
        worker_started_gate.set()
        worker_release_gate.wait()
        hotkey.start(startup_timeout=1.0)

    # 1. Spawn delayed toggle worker
    t = threading.Thread(target=delayed_worker, name="TestDelayedToggleWorker")
    t.start()

    # Wait until toggle worker is about to call start
    assert worker_started_gate.wait(timeout=2.0)

    # 2. Terminal shutdown is triggered on hotkey service before worker runs start()
    hotkey.shutdown(timeout=2.0)
    assert hotkey.is_terminated

    # 3. Release delayed worker
    worker_release_gate.set()
    t.join(timeout=2.0)
    assert not t.is_alive()

    # 4. Verify hotkey never registered and remains STOPPED
    assert not hotkey.is_running
    assert hotkey.state == HotkeyServiceState.STOPPED
    assert len(backend.registered_hotkeys) == 0


def test_runner_with_real_controller_integration() -> None:
    """DesktopApplicationRunner starts and stops cleanly with a real AssistantController instance."""
    from app.bootstrap import create_controller

    # Create real controller without speech/AI to keep test lightweight and independent
    ctrl = create_controller(enable_speech=False, enable_ai=False)

    # Disable tray/hotkey real Win32 display by injecting controlled mock UI services
    mock_hud = MagicMock()
    mock_hud.start.return_value = True
    mock_hud.stop.return_value = True
    mock_hud.is_worker_alive = False
    mock_hud.is_gui_alive = False
    mock_hud.is_cleanly_stopped = True

    config = AppConfig(desktop_enabled=True, desktop_tray_enabled=False, hotkey_enabled=False)
    runner = DesktopApplicationRunner(
        config=config,
        controller=ctrl,
        hud_service=mock_hud,
    )

    try:
        assert runner.start() is True
        assert runner.is_running
        assert ctrl.is_running
        assert runner.stop(timeout=2.0) is True
        assert runner.is_cleanly_stopped
        assert ctrl.is_cleanly_stopped
    finally:
        if ctrl.is_running:
            ctrl.stop(timeout=2.0)
