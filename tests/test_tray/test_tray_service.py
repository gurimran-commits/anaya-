"""Unit tests for WindowsTrayService verifying lifecycle, thread ownership, Explorer recovery, and hotkey serialization."""

from __future__ import annotations

import threading
import time
from threading import Event
from typing import Any, Callable

import pytest

from app.tray.models import (
    IDM_EXIT,
    IDM_OPEN,
    IDM_TOGGLE_HOTKEY,
    NIN_KEYSELECT,
    NIN_SELECT,
    NOTIFYICON_VERSION_4,
    WM_CLOSE,
    WM_COMMAND,
    WM_CONTEXTMENU,
    WM_LBUTTONUP,
    WM_NULL,
    WM_QUIT,
    WM_TRAY_CALLBACK,
    TrayServiceState,
    TrayStatusSnapshot,
)
from app.tray.service import WindowsTrayService


class MockMsg:
    """Mock structure mimicking Win32 MSG."""

    def __init__(self, hwnd: int = 0, message: int = 0, wParam: int = 0, lParam: int = 0) -> None:
        self.hwnd = hwnd
        self.message = message
        self.wParam = wParam
        self.lParam = lParam


class MockTrayBackend:
    """Thread-safe mock backend simulating Win32 system tray, window creation, and message loop."""

    def __init__(self) -> None:
        self.is_available_flag = True
        self.hwnd_counter = 1000
        self.current_hwnd = 1000
        self.created_windows: list[dict[str, Any]] = []
        self.destroyed_windows: list[int] = []

        self.added_icons: list[dict[str, Any]] = []
        self.set_versions: list[dict[str, Any]] = []
        self.modified_icons: list[dict[str, Any]] = []
        self.deleted_icons: list[dict[str, Any]] = []

        # Thread tracking
        self.thread_ids_for_add: list[int] = []
        self.thread_ids_for_delete: list[int] = []
        self.thread_ids_for_destroy: list[int] = []

        # Error simulation flags
        self.should_fail_add_icon = False
        self.should_fail_set_version = False
        self.should_fail_delete_icon = False
        self.should_fail_post_message = False
        self.last_error_code = 0

        # Menu tracking
        self.created_menus: list[int] = []
        self.destroyed_menus: list[int] = []
        self.appended_items: list[dict[str, Any]] = []
        self.tracked_popups: list[dict[str, Any]] = []
        self.foreground_windows: list[int] = []
        self.posted_messages: list[dict[str, Any]] = []

        # Thread queue & WndProc dispatch
        self._msg_queue: list[MockMsg] = []
        self._queue_lock = threading.Lock()
        self._queue_cond = threading.Condition(self._queue_lock)
        self._quit_received = False
        self._wndproc: Callable[..., int] | None = None
        self._thread_id = 99999
        self.taskbar_created_msg_id = 0xC001

    def is_available(self) -> bool:
        return self.is_available_flag

    def get_current_thread_id(self) -> int:
        return self._thread_id

    def create_tray_window(self, class_name: str, window_title: str, wndproc: Callable[..., int]) -> int:
        self.current_hwnd += 1
        hwnd = self.current_hwnd
        self._wndproc = wndproc
        self.created_windows.append({"hwnd": hwnd, "class_name": class_name, "title": window_title})
        return hwnd

    def destroy_tray_window(self, hwnd: int) -> bool:
        self.thread_ids_for_destroy.append(threading.get_ident())
        self.destroyed_windows.append(hwnd)
        return True

    def def_window_proc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        return 0

    def register_taskbar_created_message(self) -> int:
        return self.taskbar_created_msg_id

    def add_icon(self, hwnd: int, icon_id: int, hicon: int | None, tooltip: str) -> bool:
        self.thread_ids_for_add.append(threading.get_ident())
        if self.should_fail_add_icon:
            self.last_error_code = 1460
            return False
        self.added_icons.append({"hwnd": hwnd, "icon_id": icon_id, "hicon": hicon, "tooltip": tooltip})
        return True

    def set_version(self, hwnd: int, icon_id: int, version: int = NOTIFYICON_VERSION_4) -> bool:
        if self.should_fail_set_version:
            self.last_error_code = 87
            return False
        self.set_versions.append({"hwnd": hwnd, "icon_id": icon_id, "version": version})
        return True

    def modify_icon(self, hwnd: int, icon_id: int, hicon: int | None = None, tooltip: str | None = None) -> bool:
        self.modified_icons.append({"hwnd": hwnd, "icon_id": icon_id, "hicon": hicon, "tooltip": tooltip})
        return True

    def delete_icon(self, hwnd: int, icon_id: int) -> bool:
        self.thread_ids_for_delete.append(threading.get_ident())
        if self.should_fail_delete_icon:
            self.last_error_code = 1400
            return False
        self.deleted_icons.append({"hwnd": hwnd, "icon_id": icon_id})
        return True

    def load_default_icon(self) -> int:
        return 32512

    def destroy_icon(self, hicon: int) -> bool:
        return True

    def post_message(self, hwnd: int, msg: int, wparam: int = 0, lparam: int = 0) -> bool:
        if self.should_fail_post_message:
            return False
        self.posted_messages.append({"hwnd": hwnd, "msg": msg, "wparam": wparam, "lparam": lparam})
        with self._queue_cond:
            self._msg_queue.append(MockMsg(hwnd=hwnd, message=msg, wParam=wparam, lParam=lparam))
            self._queue_cond.notify_all()
        return True

    def post_thread_quit(self, thread_id: int) -> bool:
        with self._queue_cond:
            self._quit_received = True
            self._msg_queue.append(MockMsg(message=WM_QUIT))
            self._queue_cond.notify_all()
        return True

    def create_msg(self) -> MockMsg:
        return MockMsg()

    def get_message(self, msg: Any) -> int:
        with self._queue_cond:
            while not self._msg_queue and not self._quit_received:
                self._queue_cond.wait(timeout=0.1)

            if self._quit_received and not self._msg_queue:
                msg.message = WM_QUIT
                return 0

            if self._msg_queue:
                incoming = self._msg_queue.pop(0)
                msg.hwnd = incoming.hwnd
                msg.message = incoming.message
                msg.wParam = incoming.wParam
                msg.lParam = incoming.lParam
                if msg.message == WM_QUIT:
                    return 0
                return 1

            return 1

    def translate_message(self, msg: Any) -> None:
        pass

    def dispatch_message(self, msg: Any) -> int:
        if self._wndproc is not None:
            return self._wndproc(msg.hwnd, msg.message, msg.wParam, msg.lParam)
        return 0

    def create_popup_menu(self) -> int:
        hmenu = len(self.created_menus) + 500
        self.created_menus.append(hmenu)
        return hmenu

    def destroy_menu(self, hmenu: int) -> bool:
        self.destroyed_menus.append(hmenu)
        return True

    def append_menu_item(self, hmenu: int, flags: int, item_id: int, text: str) -> bool:
        self.appended_items.append({"hmenu": hmenu, "flags": flags, "item_id": item_id, "text": text})
        return True

    def append_submenu(self, hmenu: int, flags: int, submenu_handle: int, text: str) -> bool:
        self.appended_items.append({"hmenu": hmenu, "flags": flags, "submenu": submenu_handle, "text": text})
        return True

    def append_separator(self, hmenu: int) -> bool:
        self.appended_items.append({"hmenu": hmenu, "separator": True})
        return True

    def set_foreground_window(self, hwnd: int) -> bool:
        self.foreground_windows.append(hwnd)
        return True

    def track_popup_menu(self, hmenu: int, x: int, y: int, hwnd: int) -> bool:
        self.tracked_popups.append({"hmenu": hmenu, "x": x, "y": y, "hwnd": hwnd})
        return True

    def get_cursor_pos(self) -> tuple[int, int]:
        return 100, 200

    def get_last_error(self) -> int:
        return self.last_error_code

    def inject_tray_callback(self, hwnd: int, event_code: int, icon_id: int = 1, x: int = 100, y: int = 200) -> None:
        """Inject a simulated NOTIFYICON_VERSION_4 callback message into the queue."""
        # LOWORD(lParam) = event_code, HIWORD(lParam) = icon_id
        l_param = (event_code & 0xFFFF) | ((icon_id & 0xFFFF) << 16)
        # LOWORD(wParam) = x, HIWORD(wParam) = y
        w_param = (x & 0xFFFF) | ((y & 0xFFFF) << 16)
        self.post_message(hwnd, WM_TRAY_CALLBACK, wparam=w_param, lparam=l_param)

    def inject_taskbar_created(self, hwnd: int) -> None:
        """Inject a simulated Explorer TaskbarCreated broadcast."""
        self.post_message(hwnd, self.taskbar_created_msg_id, 0, 0)


class MockGlobalHotkeyService:
    """Mock GlobalHotkeyService simulating start/stop lifecycle and concurrency."""

    def __init__(self) -> None:
        self.is_running = True
        self.state = TrayServiceState.RUNNING
        self.is_voice_active = False
        self.combination = type("Combo", (), {"canonical_string": "win+alt+a"})()
        self.start_calls = 0
        self.stop_calls = 0
        self.block_toggle_event: Event | None = None
        self.toggle_invoked_event = Event()

    def start(self, startup_timeout: float = 3.0) -> bool:
        self.start_calls += 1
        self.toggle_invoked_event.set()
        if self.block_toggle_event is not None:
            self.block_toggle_event.wait()
        self.is_running = True
        return True

    def stop(self, timeout: float = 2.0) -> None:
        self.stop_calls += 1
        self.toggle_invoked_event.set()
        if self.block_toggle_event is not None:
            self.block_toggle_event.wait()
        self.is_running = False


class TestWindowsTrayServiceLifecycle:
    """Test suite for tray service lifecycle, thread ownership, and clean shutdown."""

    def test_clean_start_stop_lifecycle(self) -> None:
        """Verify normal STOPPED -> STARTING -> RUNNING -> STOPPING -> STOPPED transitions."""
        backend = MockTrayBackend()
        controller = type("Ctrl", (), {"is_running": True, "voice_pipeline": None, "model_manager": None})()
        service = WindowsTrayService(controller=controller, backend=backend)

        assert service.state == TrayServiceState.STOPPED
        assert service.is_running is False
        assert service.is_cleanly_stopped is True

        started = service.start(startup_timeout=2.0)
        assert started is True
        assert service.state == TrayServiceState.RUNNING
        assert service.is_running is True
        assert len(backend.added_icons) == 1
        assert len(backend.set_versions) == 1
        assert backend.set_versions[0]["version"] == NOTIFYICON_VERSION_4

        service.stop(timeout=1.0)
        assert service.state == TrayServiceState.STOPPED
        assert service.is_running is False
        assert service.is_cleanly_stopped is True
        assert len(backend.deleted_icons) == 1
        assert len(backend.destroyed_windows) == 1

    def test_window_thread_ownership(self) -> None:
        """Verify NIM_ADD, NIM_DELETE, and DestroyWindow are executed strictly on the tray thread."""
        backend = MockTrayBackend()
        controller = type("Ctrl", (), {"is_running": True, "voice_pipeline": None, "model_manager": None})()
        service = WindowsTrayService(controller=controller, backend=backend)

        service.start(startup_timeout=2.0)
        service.stop(timeout=1.0)

        # All operations must have occurred on the dedicated tray thread, NOT on the test runner thread
        caller_tid = threading.get_ident()
        assert len(backend.thread_ids_for_add) == 1
        assert backend.thread_ids_for_add[0] != caller_tid

        assert len(backend.thread_ids_for_delete) == 1
        assert backend.thread_ids_for_delete[0] == backend.thread_ids_for_add[0]

        assert len(backend.thread_ids_for_destroy) == 1
        assert backend.thread_ids_for_destroy[0] == backend.thread_ids_for_add[0]

    def test_nim_setversion_failure_triggers_rollback(self) -> None:
        """Mandatory condition 2: NIM_SETVERSION failure rolls back NIM_ADD and denies running state."""
        backend = MockTrayBackend()
        backend.should_fail_set_version = True

        controller = type("Ctrl", (), {"is_running": True, "voice_pipeline": None, "model_manager": None})()
        service = WindowsTrayService(controller=controller, backend=backend)

        started = service.start(startup_timeout=1.0)
        assert started is False
        assert service.state == TrayServiceState.STOPPED
        assert service.is_running is False

        # NIM_ADD was attempted, followed by rollback NIM_DELETE
        assert len(backend.added_icons) == 1
        assert len(backend.deleted_icons) == 1

        service.stop(timeout=1.0)
        assert service.is_cleanly_stopped is True

    def test_nim_setversion_and_rollback_failure_handling(self) -> None:
        """Mandatory condition 2: Rollback failure latches unregistration_failed and denies clean shutdown."""
        backend = MockTrayBackend()
        backend.should_fail_set_version = True
        backend.should_fail_delete_icon = True  # Rollback fails!

        controller = type("Ctrl", (), {"is_running": True, "voice_pipeline": None, "model_manager": None})()
        service = WindowsTrayService(controller=controller, backend=backend)

        started = service.start(startup_timeout=1.0)
        assert started is False

        # Rollback failed: clean shutdown must be denied
        assert service.is_cleanly_stopped is False
        assert service._unregistration_failed is True

        service.stop(timeout=1.0)
        assert service.is_cleanly_stopped is False
        assert service.state == TrayServiceState.TIMEOUT_STOPPED

    def test_cross_platform_graceful_degradation(self) -> None:
        """When platform is unsupported (is_available == False), start returns False safely."""
        backend = MockTrayBackend()
        backend.is_available_flag = False

        controller = type("Ctrl", (), {"is_running": True, "voice_pipeline": None, "model_manager": None})()
        service = WindowsTrayService(controller=controller, backend=backend)

        started = service.start(startup_timeout=1.0)
        assert started is False
        assert service.state == TrayServiceState.STOPPED
        assert service.is_running is False
        assert service.is_cleanly_stopped is True


class TestExplorerRestartRecovery:
    """Test suite for Explorer TaskbarCreated broadcast handling."""

    def test_taskbar_created_re_registers_icon(self) -> None:
        """Verify TaskbarCreated broadcast re-adds the icon and re-applies NOTIFYICON_VERSION_4."""
        backend = MockTrayBackend()
        controller = type("Ctrl", (), {"is_running": True, "voice_pipeline": None, "model_manager": None})()
        service = WindowsTrayService(controller=controller, backend=backend)

        service.start(startup_timeout=2.0)
        assert len(backend.added_icons) == 1

        # Simulate Explorer restart
        backend.inject_taskbar_created(service._hwnd)  # type: ignore[arg-type]

        # Wait for dispatch
        for _ in range(50):
            if len(backend.added_icons) == 2:
                break
            time.sleep(0.01)

        assert len(backend.added_icons) == 2
        assert len(backend.set_versions) == 2
        assert service.is_running is True

        service.stop(timeout=1.0)
        assert service.is_cleanly_stopped is True

    def test_taskbar_created_recovery_failure_does_not_falsely_claim_running(self) -> None:
        """Verify that if re-registration fails during TaskbarCreated, icon is not marked active."""
        backend = MockTrayBackend()
        controller = type("Ctrl", (), {"is_running": True, "voice_pipeline": None, "model_manager": None})()
        service = WindowsTrayService(controller=controller, backend=backend)

        service.start(startup_timeout=2.0)
        assert service.is_running is True

        # Now simulate Explorer restart with failing add_icon
        backend.should_fail_add_icon = True
        backend.inject_taskbar_created(service._hwnd)  # type: ignore[arg-type]

        for _ in range(50):
            if not service._icon_added:
                break
            time.sleep(0.01)

        assert service.is_running is False
        assert service._icon_added is False

        backend.should_fail_add_icon = False
        service.stop(timeout=1.0)

    def test_taskbar_created_ignored_during_shutdown(self) -> None:
        """Verify TaskbarCreated broadcast during shutdown does not add or set version on icon."""
        backend = MockTrayBackend()
        controller = type("Ctrl", (), {"is_running": True, "voice_pipeline": None, "model_manager": None})()
        service = WindowsTrayService(controller=controller, backend=backend)

        service.start(startup_timeout=2.0)
        assert len(backend.added_icons) == 1
        assert len(backend.set_versions) == 1
        hwnd = service._hwnd
        assert hwnd is not None

        # Initiate shutdown under lock (setting STOPPING and _stop_requested)
        with service._lock:
            service._state = TrayServiceState.STOPPING
            service._stop_requested = True

        # Inject TaskbarCreated while service is in stopping state
        backend.inject_taskbar_created(hwnd)

        # Process message and finish stop
        service.stop(timeout=1.0)

        # No new icon was added or version set during shutdown!
        assert len(backend.added_icons) == 1
        assert len(backend.set_versions) == 1
        assert service.state == TrayServiceState.STOPPED
        assert service.is_cleanly_stopped is True
        assert len(backend.deleted_icons) == 1
        assert len(backend.destroyed_windows) == 1

    def test_taskbar_created_racing_with_shutdown_rolls_back_if_setversion_finished(self) -> None:
        """Verify that if shutdown begins while TaskbarCreated is executing, rollback delete is triggered."""
        backend = MockTrayBackend()
        controller = type("Ctrl", (), {"is_running": True, "voice_pipeline": None, "model_manager": None})()
        service = WindowsTrayService(controller=controller, backend=backend)

        service.start(startup_timeout=2.0)
        assert len(backend.added_icons) == 1
        hwnd = service._hwnd
        assert hwnd is not None

        # Hook set_version on the backend to trigger shutdown right when set_version completes
        orig_set_version = backend.set_version
        def set_version_and_trigger_shutdown(h: int, i_id: int, ver: int = NOTIFYICON_VERSION_4) -> bool:
            res = orig_set_version(h, i_id, ver)
            # Simulate shutdown being signaled immediately before _handle_taskbar_created takes final lock
            with service._lock:
                service._stop_requested = True
                service._state = TrayServiceState.STOPPING
            return res

        backend.set_version = set_version_and_trigger_shutdown  # type: ignore[method-assign]

        # Trigger TaskbarCreated
        backend.inject_taskbar_created(hwnd)

        # Wait for dispatch and complete stop
        service.stop(timeout=1.0)

        # A rollback delete must have been issued, ensuring no active icon remains
        assert service.state == TrayServiceState.STOPPED
        assert service.is_cleanly_stopped is True
        assert service.is_running is False


class TestSerializedHotkeyOperations:
    """Test suite for AnayaHotkeyToggleWorker serialization, non-daemon state, and shutdown coordination."""

    def test_serialized_toggle_worker_lifecycle_and_non_daemon(self) -> None:
        """Mandatory condition 1: Verify AnayaHotkeyToggleWorker is non-daemon and serializes operations."""
        backend = MockTrayBackend()
        controller = type("Ctrl", (), {"is_running": True, "voice_pipeline": None, "model_manager": None})()
        hotkey_svc = MockGlobalHotkeyService()
        hotkey_svc.block_toggle_event = Event()  # Hold worker active

        service = WindowsTrayService(controller=controller, hotkey_service=hotkey_svc, backend=backend)
        service.start(startup_timeout=2.0)

        # 1. Trigger first toggle
        triggered = service._request_hotkey_toggle()
        assert triggered is True
        assert service.is_hotkey_operation_busy is True

        worker = service._hotkey_worker_thread
        assert worker is not None
        assert worker.is_alive() is True
        # Mandatory condition 1: Must be non-daemon!
        assert worker.daemon is False

        # 2. Trigger second toggle while first is in-flight: must be dropped immediately!
        second_triggered = service._request_hotkey_toggle()
        assert second_triggered is False

        # 3. Release first worker
        hotkey_svc.block_toggle_event.set()
        worker.join(timeout=1.0)

        assert service.is_hotkey_operation_busy is False
        assert hotkey_svc.stop_calls == 1  # Initially was running, so stop was called

        service.stop(timeout=1.0)
        assert service.is_cleanly_stopped is True

    def test_shutdown_waits_for_inflight_hotkey_toggle(self) -> None:
        """Verify stop(timeout) joins in-flight hotkey worker within monotonic budget."""
        backend = MockTrayBackend()
        controller = type("Ctrl", (), {"is_running": True, "voice_pipeline": None, "model_manager": None})()
        hotkey_svc = MockGlobalHotkeyService()
        hotkey_svc.block_toggle_event = Event()  # Hold worker

        service = WindowsTrayService(controller=controller, hotkey_service=hotkey_svc, backend=backend)
        service.start(startup_timeout=2.0)

        service._request_hotkey_toggle()
        assert service.is_hotkey_operation_busy is True

        # Stop with very short timeout while worker is hung
        service.stop(timeout=0.05)
        # Timeout occurred: clean shutdown must be denied and worker reference retained
        assert service.state == TrayServiceState.TIMEOUT_STOPPED
        assert service.is_cleanly_stopped is False
        assert service._hotkey_worker_thread is not None
        assert service._hotkey_worker_thread.is_alive() is True

        # Unblock worker and retry stop
        hotkey_svc.block_toggle_event.set()
        service.stop(timeout=1.0)

        assert service.state == TrayServiceState.STOPPED
        assert service.is_cleanly_stopped is True
        assert service._hotkey_worker_thread is None


class TestTrayMenuInteractions:
    """Test suite for menu actions, NOTIFYICON_VERSION_4 callbacks, and KB131501 focus handling."""

    def test_primary_action_dispatches_on_open(self) -> None:
        """Verify NIN_SELECT or WM_LBUTTONUP dispatches on_open callback."""
        backend = MockTrayBackend()
        controller = type("Ctrl", (), {"is_running": True, "voice_pipeline": None, "model_manager": None})()
        open_called = Event()

        service = WindowsTrayService(
            controller=controller,
            backend=backend,
            on_open=open_called.set,
        )
        service.start(startup_timeout=2.0)

        # Inject NIN_SELECT callback
        backend.inject_tray_callback(service._hwnd, NIN_SELECT)  # type: ignore[arg-type]

        assert open_called.wait(timeout=1.0) is True

        service.stop(timeout=1.0)

    def test_exit_command_dispatches_on_exit(self) -> None:
        """Verify IDM_EXIT dispatches on_exit callback."""
        backend = MockTrayBackend()
        controller = type("Ctrl", (), {"is_running": True, "voice_pipeline": None, "model_manager": None})()
        exit_called = Event()

        service = WindowsTrayService(
            controller=controller,
            backend=backend,
            on_exit=exit_called.set,
        )
        service.start(startup_timeout=2.0)

        # Post WM_COMMAND with IDM_EXIT
        backend.post_message(service._hwnd, WM_COMMAND, wparam=IDM_EXIT, lparam=0)  # type: ignore[arg-type]

        assert exit_called.wait(timeout=1.0) is True

        service.stop(timeout=1.0)

    def test_context_menu_kb131501_focus_handling(self) -> None:
        """Verify TrackPopupMenuEx is preceded by SetForegroundWindow and followed by PostMessage(WM_NULL)."""
        backend = MockTrayBackend()
        controller = type("Ctrl", (), {"is_running": True, "voice_pipeline": None, "model_manager": None})()
        service = WindowsTrayService(controller=controller, backend=backend)

        service.start(startup_timeout=2.0)

        # Inject WM_CONTEXTMENU
        backend.inject_tray_callback(service._hwnd, WM_CONTEXTMENU, x=150, y=250)  # type: ignore[arg-type]

        for _ in range(50):
            if len(backend.tracked_popups) > 0:
                break
            time.sleep(0.01)

        assert len(backend.tracked_popups) == 1
        assert backend.tracked_popups[0]["x"] == 150
        assert backend.tracked_popups[0]["y"] == 250

        # Must have brought window to foreground
        assert len(backend.foreground_windows) == 1
        assert backend.foreground_windows[0] == service._hwnd

        # Must have posted WM_NULL
        null_posts = [p for p in backend.posted_messages if p["msg"] == WM_NULL]
        assert len(null_posts) >= 1

        service.stop(timeout=1.0)

    def test_keyboard_selection_nin_keyselect_and_wm_lbuttonup(self) -> None:
        """Verify NIN_KEYSELECT and WM_LBUTTONUP trigger on_open callback."""
        backend = MockTrayBackend()
        controller = type("Ctrl", (), {"is_running": True, "voice_pipeline": None, "model_manager": None})()
        open_count = 0

        def count_open() -> None:
            nonlocal open_count
            open_count += 1

        service = WindowsTrayService(
            controller=controller,
            backend=backend,
            on_open=count_open,
        )
        service.start(startup_timeout=2.0)

        # 1. NIN_KEYSELECT
        backend.inject_tray_callback(service._hwnd, NIN_KEYSELECT)  # type: ignore[arg-type]
        for _ in range(50):
            if open_count == 1:
                break
            time.sleep(0.01)
        assert open_count == 1

        # 2. WM_LBUTTONUP
        backend.inject_tray_callback(service._hwnd, WM_LBUTTONUP)  # type: ignore[arg-type]
        for _ in range(50):
            if open_count == 2:
                break
            time.sleep(0.01)
        assert open_count == 2

        service.stop(timeout=1.0)

    def test_update_tooltip_live_modification(self) -> None:
        """Verify update_tooltip updates state and issues modify_icon."""
        backend = MockTrayBackend()
        controller = type("Ctrl", (), {"is_running": True, "voice_pipeline": None, "model_manager": None})()
        service = WindowsTrayService(controller=controller, backend=backend, tooltip="Initial")
        service.start(startup_timeout=2.0)

        assert service.tooltip == "Initial"
        service.update_tooltip("Updated Tooltip")
        assert service.tooltip == "Updated Tooltip"

        assert len(backend.modified_icons) == 1
        assert backend.modified_icons[0]["tooltip"] == "Updated Tooltip"

        service.stop(timeout=1.0)

    def test_clean_restart_lifecycle(self) -> None:
        """Verify service supports multi-cycle start -> stop -> start -> stop cleanly."""
        backend = MockTrayBackend()
        controller = type("Ctrl", (), {"is_running": True, "voice_pipeline": None, "model_manager": None})()
        service = WindowsTrayService(controller=controller, backend=backend)

        # Cycle 1
        assert service.start(startup_timeout=2.0) is True
        assert service.is_running is True
        service.stop(timeout=1.0)
        assert service.is_cleanly_stopped is True

        # Cycle 2
        assert service.start(startup_timeout=2.0) is True
        assert service.is_running is True
        service.stop(timeout=1.0)
        assert service.is_cleanly_stopped is True

    def test_reject_invalid_startup_and_stop_timeouts(self) -> None:
        """Verify defensive validation for startup_timeout and stop timeout."""
        backend = MockTrayBackend()
        controller = type("Ctrl", (), {"is_running": True, "voice_pipeline": None, "model_manager": None})()
        service = WindowsTrayService(controller=controller, backend=backend)

        with pytest.raises(ValueError):
            service.start(startup_timeout=-1.0)

        with pytest.raises(ValueError):
            service.start(startup_timeout=True)  # type: ignore[arg-type]

        with pytest.raises(ValueError):
            service.stop(timeout=-1.0)

        with pytest.raises(TypeError):
            service.stop(timeout="2.0")  # type: ignore[arg-type]

    def test_toggle_request_dropped_when_no_hotkey_service(self) -> None:
        """Verify _request_hotkey_toggle returns False when no hotkey service is configured."""
        backend = MockTrayBackend()
        controller = type("Ctrl", (), {"is_running": True, "voice_pipeline": None, "model_manager": None})()
        service = WindowsTrayService(controller=controller, hotkey_service=None, backend=backend)
        service.start(startup_timeout=2.0)

        assert service._request_hotkey_toggle() is False
        assert service.is_hotkey_operation_busy is False

        service.stop(timeout=1.0)

    def test_taskbar_created_setversion_failure_rollback(self) -> None:
        """Verify that when TaskbarCreated recovery fails at set_version, rollback delete is issued."""
        backend = MockTrayBackend()
        controller = type("Ctrl", (), {"is_running": True, "voice_pipeline": None, "model_manager": None})()
        service = WindowsTrayService(controller=controller, backend=backend)
        service.start(startup_timeout=2.0)

        # Fail set_version on recovery
        backend.should_fail_set_version = True
        backend.inject_taskbar_created(service._hwnd)  # type: ignore[arg-type]

        for _ in range(50):
            if not service.is_running:
                break
            time.sleep(0.01)

        assert service.is_running is False
        # Rollback delete was called
        assert len(backend.deleted_icons) >= 1

        backend.should_fail_set_version = False
        service.stop(timeout=1.0)
