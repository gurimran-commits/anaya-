"""Windows system tray service managing dedicated UI thread, icon lifecycle, and serialized hotkey operations."""

from __future__ import annotations

import logging
import math
import sys
import time
from threading import Event, Lock, RLock, Thread
from typing import Any, Callable

from app.tray.backend import TrayBackend, Win32TrayBackend
from app.tray.models import (
    IDM_EXIT,
    IDM_OPEN,
    IDM_STATUS_AI,
    IDM_STATUS_ASSISTANT,
    IDM_STATUS_HEADER,
    IDM_STATUS_HOTKEY,
    IDM_STATUS_SPEECH,
    IDM_TOGGLE_HOTKEY,
    MF_CHECKED,
    MF_DEFAULT,
    MF_GRAYED,
    MF_STRING,
    MF_UNCHECKED,
    NIN_KEYSELECT,
    NIN_SELECT,
    NOTIFYICON_VERSION_4,
    WM_CLOSE,
    WM_COMMAND,
    WM_CONTEXTMENU,
    WM_DESTROY,
    WM_LBUTTONUP,
    WM_NULL,
    WM_TRAY_CALLBACK,
    WM_TRAY_REFRESH,
    TrayRegistrationError,
    TrayServiceState,
    TrayStatusSnapshot,
    TrayVersionError,
    capture_status_snapshot,
    sanitize_and_truncate_tooltip,
)

logger = logging.getLogger(__name__)


class WindowsTrayService:
    """Windows system tray service providing unobtrusive desktop presence and status control.

    Guarantees:
        - Win32 window-thread ownership: The dedicated tray UI thread exclusively creates the window,
          processes window messages, runs Shell_NotifyIconW, and destroys the window.
        - Non-blocking tray UI: The tray message pump never blocks on controller or hotkey operations.
        - Serialized hotkey lifecycle worker: Exactly one non-daemon worker at a time handles hotkey
          enable/disable requests; rapid clicks are dropped atomically.
        - Version 4 callback layout: Decodes notification event from LOWORD(lParam), icon ID from
          HIWORD(lParam), and packed coordinates from signed wParam.
        - Explorer restart recovery: Subscribes to TaskbarCreated and re-adds the icon on shell restarts.
        - Monotonic bounded shutdown: stop(timeout) enforces a single monotonic deadline across joins.
    """

    def __init__(
        self,
        controller: Any,
        hotkey_service: Any = None,
        backend: TrayBackend | None = None,
        on_open: Callable[[], None] | None = None,
        on_exit: Callable[[], None] | None = None,
        tooltip: str = "Anaya AI Assistant",
        icon_id: int = 1,
    ) -> None:
        self._controller = controller
        self._hotkey_service = hotkey_service
        self._backend = backend or Win32TrayBackend()
        self._on_open = on_open
        self._on_exit = on_exit
        self._initial_tooltip = tooltip
        self._tooltip = sanitize_and_truncate_tooltip(tooltip)
        self._icon_id = int(icon_id)

        # Lifecycle synchronization
        self._lock = RLock()
        self._state = TrayServiceState.STOPPED
        self._stop_requested = False
        self._unregistration_failed = False
        self._icon_added = False
        self._window_destroyed = False

        self._startup_event = Event()
        self._tray_thread: Thread | None = None
        self._tray_thread_id: int | None = None
        self._hwnd: int | None = None
        self._msg_taskbar_created: int | None = None

        # Dedicated serialized hotkey toggle worker (daemon=False)
        self._hotkey_worker_thread: Thread | None = None
        self._hotkey_op_busy = False
        self._hotkey_op_lock = Lock()

    @property
    def state(self) -> TrayServiceState:
        """Current lifecycle state."""
        with self._lock:
            return self._state

    @property
    def is_running(self) -> bool:
        """True while the tray service is active with a registered icon."""
        with self._lock:
            return self._state == TrayServiceState.RUNNING and self._icon_added

    @property
    def is_cleanly_stopped(self) -> bool:
        """True only when tray thread and hotkey worker have terminated cleanly and unregistration succeeded."""
        with self._lock:
            return (
                self._state == TrayServiceState.STOPPED
                and self._tray_thread is None
                and self._hotkey_worker_thread is None
                and not self._unregistration_failed
                and not self._icon_added
            )

    @property
    def is_hotkey_operation_busy(self) -> bool:
        """True while an asynchronous hotkey toggle operation is in-flight."""
        with self._hotkey_op_lock:
            return self._hotkey_op_busy

    @property
    def tooltip(self) -> str:
        """Current tooltip text."""
        with self._lock:
            return self._tooltip

    def start(self, startup_timeout: float = 3.0) -> bool:
        """Start the dedicated tray UI thread and register the system tray icon.

        Returns:
            True if registration and version setup succeeded and service is RUNNING.
            False if backend is unavailable, registration failed, or startup timed out.
        """
        if isinstance(startup_timeout, bool) or not isinstance(startup_timeout, (int, float)):
            raise ValueError(f"startup_timeout must be a non-negative number, got {type(startup_timeout).__name__}")
        if not math.isfinite(startup_timeout) or startup_timeout < 0.0:
            raise ValueError(f"startup_timeout must be a non-negative finite number, got {startup_timeout}")

        with self._lock:
            if self._state == TrayServiceState.RUNNING:
                return True
            if self._state != TrayServiceState.STOPPED:
                return False

            if (self._tray_thread is not None and self._tray_thread.is_alive()) or self.is_hotkey_operation_busy:
                return False
            if self._unregistration_failed:
                return False

            if not self._backend.is_available():
                logger.info("Tray backend is unavailable on platform '%s'. Tray service disabled.", sys.platform)
                return False

            # Reset state for fresh run
            self._state = TrayServiceState.STARTING
            self._stop_requested = False
            self._unregistration_failed = False
            self._icon_added = False
            self._tray_thread = None
            self._tray_thread_id = None
            self._hwnd = None
            self._startup_event.clear()

            self._tray_thread = Thread(
                target=self._run_tray_ui_thread,
                name="AnayaTrayUIThread",
                daemon=True,
            )
            self._tray_thread.start()

        started = self._startup_event.wait(timeout=startup_timeout)
        if not started:
            logger.warning("Tray service startup timed out after %.1fs. Requesting cancellation.", startup_timeout)
            with self._lock:
                self._state = TrayServiceState.TIMEOUT_STOPPED
                self._stop_requested = True
                hwnd = self._hwnd
                tray_tid = self._tray_thread_id

            if hwnd is not None:
                try:
                    self._backend.post_message(hwnd, WM_CLOSE, 0, 0)
                except Exception:
                    pass
            elif tray_tid is not None:
                try:
                    self._backend.post_thread_quit(tray_tid)
                except Exception:
                    pass

            return False

        with self._lock:
            return self._state == TrayServiceState.RUNNING and self._icon_added

    def stop(self, timeout: float = 2.0) -> None:
        """Stop the tray service, delete tray icon, destroy hidden window, and join threads within deadline."""
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise TypeError(f"timeout must be a non-negative number, got {type(timeout).__name__}")
        if not math.isfinite(timeout) or timeout < 0.0:
            raise ValueError(f"timeout must be a non-negative finite number, got {timeout}")

        deadline = time.monotonic() + float(timeout)

        with self._lock:
            if (
                self._state == TrayServiceState.STOPPED
                and self._tray_thread is None
                and self._hotkey_worker_thread is None
                and not self._unregistration_failed
                and not self._icon_added
            ):
                return
            self._state = TrayServiceState.STOPPING
            self._stop_requested = True
            hwnd = self._hwnd
            tray_tid = self._tray_thread_id

        # 1. Join in-flight hotkey worker thread within monotonic budget (daemon=False)
        worker = self._hotkey_worker_thread
        if worker is not None:
            if worker.is_alive():
                remaining = max(0.0, deadline - time.monotonic())
                worker.join(timeout=remaining)
            if not worker.is_alive():
                with self._lock:
                    if self._hotkey_worker_thread is worker:
                        self._hotkey_worker_thread = None
            else:
                logger.warning("Hotkey toggle worker did not terminate within shutdown budget.")

        # 2. Signal tray UI thread to delete icon and destroy window via WM_CLOSE
        if hwnd is not None:
            posted = False
            for attempt in range(3):
                try:
                    if self._backend.post_message(hwnd, WM_CLOSE, 0, 0):
                        posted = True
                        break
                except Exception:
                    pass
                remaining_budget = deadline - time.monotonic()
                if remaining_budget <= 0.0 or attempt == 2:
                    break
                time.sleep(min(0.005, remaining_budget))

            if not posted and tray_tid is not None:
                try:
                    self._backend.post_thread_quit(tray_tid)
                except Exception:
                    pass

        # 3. Join tray UI thread within remaining monotonic budget
        tray_thread = self._tray_thread
        if tray_thread is not None:
            if tray_thread.is_alive():
                remaining = max(0.0, deadline - time.monotonic())
                tray_thread.join(timeout=remaining)
            if not tray_thread.is_alive():
                with self._lock:
                    if self._tray_thread is tray_thread:
                        self._tray_thread = None
            else:
                logger.warning("Tray UI thread did not terminate within shutdown budget.")

        # 4. Evaluate final clean shutdown state
        with self._lock:
            if (
                self._tray_thread is None
                and self._hotkey_worker_thread is None
                and not self._unregistration_failed
                and not self._icon_added
            ):
                self._state = TrayServiceState.STOPPED
                logger.debug("WindowsTrayService stopped cleanly.")
            else:
                self._state = TrayServiceState.TIMEOUT_STOPPED
                logger.warning(
                    "WindowsTrayService stop incomplete (tray_alive=%s, worker_alive=%s, unreg_failed=%s, icon_added=%s).",
                    self._tray_thread is not None,
                    self._hotkey_worker_thread is not None,
                    self._unregistration_failed,
                    self._icon_added,
                )

    def update_tooltip(self, text: str) -> None:
        """Update tray icon tooltip safely across threads."""
        sanitized = sanitize_and_truncate_tooltip(text)
        with self._lock:
            self._tooltip = sanitized
            hwnd = self._hwnd
            icon_added = self._icon_added

        if hwnd is not None and icon_added:
            try:
                self._backend.modify_icon(hwnd, self._icon_id, tooltip=sanitized)
            except Exception as exc:
                logger.debug("Error updating tray tooltip: %s", exc)

    def _request_hotkey_toggle(self) -> bool:
        """Request hotkey enable/disable. Drops requests atomically if a toggle is already in progress."""
        with self._lock:
            if self._stop_requested or self._state != TrayServiceState.RUNNING:
                return False

        with self._hotkey_op_lock:
            if self._hotkey_op_busy:
                logger.debug("Hotkey toggle request dropped: toggle operation already in-flight.")
                return False

            if self._hotkey_service is None:
                logger.debug("Hotkey toggle request dropped: no GlobalHotkeyService configured.")
                return False

            self._hotkey_op_busy = True
            # Spawn dedicated single non-daemon worker
            self._hotkey_worker_thread = Thread(
                target=self._run_hotkey_toggle_worker,
                name="AnayaHotkeyToggleWorker",
                daemon=False,
            )
            self._hotkey_worker_thread.start()
            return True

    def _run_hotkey_toggle_worker(self) -> None:
        """Dedicated worker executing serialized start/stop on GlobalHotkeyService."""
        try:
            hk = self._hotkey_service
            if hk is not None:
                if hk.is_running:
                    logger.info("Tray menu disabling global hotkey.")
                    hk.stop(timeout=2.0)
                else:
                    logger.info("Tray menu enabling global hotkey.")
                    hk.start(startup_timeout=3.0)

            # Signal tray UI thread to update menu checkmark and tooltip
            with self._lock:
                if not self._stop_requested and self._hwnd is not None:
                    hwnd = self._hwnd
                else:
                    hwnd = None

            if hwnd is not None:
                try:
                    self._backend.post_message(hwnd, WM_TRAY_REFRESH, 0, 0)
                except Exception:
                    pass
        except Exception as exc:
            logger.error("Unhandled exception in hotkey toggle worker: %s", exc, exc_info=True)
        finally:
            with self._hotkey_op_lock:
                self._hotkey_op_busy = False

    def _run_tray_ui_thread(self) -> None:
        """Dedicated UI thread owning the hidden HWND, Shell_NotifyIconW, and message pump."""
        class_name = f"AnayaTrayWindowClass_{id(self)}"
        window_title = "AnayaTrayWindow"
        hwnd: int | None = None
        registration_succeeded = False

        try:
            self._tray_thread_id = self._backend.get_current_thread_id()
            self._msg_taskbar_created = self._backend.register_taskbar_created_message()

            hwnd = self._backend.create_tray_window(class_name, window_title, self._wndproc)
            with self._lock:
                self._hwnd = hwnd

            # Add icon via NIM_ADD
            hicon = self._backend.load_default_icon()
            added = self._backend.add_icon(hwnd, self._icon_id, hicon, self._tooltip)
            if not added:
                err = self._backend.get_last_error()
                logger.error("Failed to add tray icon via Shell_NotifyIconW (Win32 error %d).", err)
                raise TrayRegistrationError(f"Failed to add tray icon (Win32 error {err}).")

            # Set version via NIM_SETVERSION (NOTIFYICON_VERSION_4)
            version_set = self._backend.set_version(hwnd, self._icon_id, NOTIFYICON_VERSION_4)
            if not version_set:
                err = self._backend.get_last_error()
                logger.error("Failed to set NOTIFYICON_VERSION_4 (Win32 error %d). Rolling back NIM_ADD.", err)
                # Rollback via NIM_DELETE
                rollback_ok = self._backend.delete_icon(hwnd, self._icon_id)
                if not rollback_ok:
                    logger.error("Rollback of NIM_ADD failed (Win32 error %d).", self._backend.get_last_error())
                    with self._lock:
                        self._unregistration_failed = True
                raise TrayVersionError(f"Failed to set NOTIFYICON_VERSION_4 (Win32 error {err}).")

            registration_succeeded = True
            with self._lock:
                self._icon_added = True
                self._state = TrayServiceState.RUNNING

            self._startup_event.set()
            logger.info("WindowsTrayService initialized and running.")

            # Standard Win32 Window Message Pump
            msg = self._backend.create_msg()
            while not self._stop_requested:
                ret = self._backend.get_message(msg)
                if ret == 0 or ret == -1:  # WM_QUIT or error
                    break
                self._backend.translate_message(msg)
                self._backend.dispatch_message(msg)

        except Exception as exc:
            logger.error("Fatal error in AnayaTrayUIThread: %s", exc, exc_info=True)
            with self._lock:
                if not registration_succeeded:
                    self._state = TrayServiceState.STOPPED
                else:
                    self._state = TrayServiceState.TIMEOUT_STOPPED
        finally:
            self._startup_event.set()

            # Guaranteed unregistration and destruction strictly on tray UI thread
            if hwnd is not None:
                with self._lock:
                    icon_was_added = self._icon_added

                if icon_was_added:
                    try:
                        deleted = self._backend.delete_icon(hwnd, self._icon_id)
                        if not deleted:
                            logger.error(
                                "Failed to delete tray icon on UI thread exit (Win32 error %d).",
                                self._backend.get_last_error(),
                            )
                            with self._lock:
                                self._unregistration_failed = True
                        else:
                            with self._lock:
                                self._icon_added = False
                            logger.debug("Tray icon deleted cleanly on UI thread exit.")
                    except Exception as del_exc:
                        logger.error("Exception deleting tray icon: %s", del_exc, exc_info=True)
                        with self._lock:
                            self._unregistration_failed = True

                with self._lock:
                    window_already_destroyed = self._window_destroyed
                if not window_already_destroyed:
                    try:
                        self._backend.destroy_tray_window(hwnd)
                        with self._lock:
                            self._window_destroyed = True
                    except Exception as destroy_exc:
                        logger.debug("Exception destroying tray window: %s", destroy_exc)

            with self._lock:
                self._hwnd = None
                self._icon_added = False

    def _wndproc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        """Native Window Procedure handling tray callbacks, menus, and Explorer restarts."""
        # 1. Explorer taskbar recreation broadcast
        if self._msg_taskbar_created is not None and msg == self._msg_taskbar_created:
            self._handle_taskbar_created(hwnd)
            return 0

        # 2. Tray notification callback (NOTIFYICON_VERSION_4)
        if msg == WM_TRAY_CALLBACK:
            event_code = lparam & 0xFFFF
            icon_id = (lparam >> 16) & 0xFFFF
            if icon_id == self._icon_id:
                # Signed coordinates from packed wParam
                raw_x = wparam & 0xFFFF
                raw_y = (wparam >> 16) & 0xFFFF
                import ctypes
                x = ctypes.c_short(raw_x).value
                y = ctypes.c_short(raw_y).value

                if event_code == WM_CONTEXTMENU:
                    self._show_context_menu(hwnd, x, y)
                    return 0
                elif event_code in (NIN_SELECT, NIN_KEYSELECT, WM_LBUTTONUP):
                    self._handle_primary_action()
                    return 0
            return 0

        # 3. Context menu command dispatch
        if msg == WM_COMMAND:
            cmd_id = wparam & 0xFFFF
            self._handle_menu_command(cmd_id)
            return 0

        # 4. Tray UI refresh signal from background worker
        if msg == WM_TRAY_REFRESH:
            self._handle_refresh(hwnd)
            return 0

        # 5. Clean window destruction messages
        if msg == WM_CLOSE:
            # Unregister icon and destroy window on this thread
            with self._lock:
                icon_was_added = self._icon_added
            if icon_was_added:
                try:
                    deleted = self._backend.delete_icon(hwnd, self._icon_id)
                    if not deleted:
                        with self._lock:
                            self._unregistration_failed = True
                    else:
                        with self._lock:
                            self._icon_added = False
                except Exception:
                    with self._lock:
                        self._unregistration_failed = True

            self._backend.destroy_tray_window(hwnd)
            with self._lock:
                self._window_destroyed = True
            return 0

        if msg == WM_DESTROY:
            self._backend.post_thread_quit(self._tray_thread_id or 0)
            return 0

        return self._backend.def_window_proc(hwnd, msg, wparam, lparam)

    def _handle_taskbar_created(self, hwnd: int) -> None:
        """Handle Windows Explorer crash/restart by re-registering the tray icon safely."""
        with self._lock:
            if self._stop_requested or self._state != TrayServiceState.RUNNING:
                logger.debug("Ignoring TaskbarCreated broadcast: service is stopping or not running.")
                return
            # Mark previous shell registration invalid
            self._icon_added = False

        logger.info("TaskbarCreated received. Re-registering system tray icon.")

        hicon = self._backend.load_default_icon()
        added = self._backend.add_icon(hwnd, self._icon_id, hicon, self._tooltip)
        if not added:
            err = self._backend.get_last_error()
            logger.warning("Failed to re-add tray icon following Explorer restart (Win32 error %d).", err)
            return

        version_set = self._backend.set_version(hwnd, self._icon_id, NOTIFYICON_VERSION_4)
        if not version_set:
            err = self._backend.get_last_error()
            logger.warning("Failed to re-apply NOTIFYICON_VERSION_4 after Explorer restart (Win32 error %d).", err)
            rollback_ok = self._backend.delete_icon(hwnd, self._icon_id)
            if not rollback_ok:
                with self._lock:
                    self._unregistration_failed = True
            return

        with self._lock:
            if self._stop_requested or self._state != TrayServiceState.RUNNING:
                logger.debug("Shutdown initiated during Explorer recovery; rolling back re-added icon.")
                rollback_ok = self._backend.delete_icon(hwnd, self._icon_id)
                if not rollback_ok:
                    self._unregistration_failed = True
                self._icon_added = False
                return

            self._icon_added = True
        logger.info("System tray icon recovered successfully after Explorer restart.")

    def _show_context_menu(self, hwnd: int, x: int, y: int) -> None:
        """Build and display the Win32 popup context menu at cursor coordinates."""
        snapshot = capture_status_snapshot(self._controller, self._hotkey_service)

        # Fallback to cursor pos if coordinates are invalid (e.g. keyboard menu key)
        if x == -1 and y == -1:
            x, y = self._backend.get_cursor_pos()

        hmenu = self._backend.create_popup_menu()
        if not hmenu:
            return

        try:
            # 1. Open Anaya (Default item, bold)
            self._backend.append_menu_item(hmenu, MF_STRING | MF_DEFAULT, IDM_OPEN, "Open Anaya")
            self._backend.append_separator(hmenu)

            # 2. Hotkey Toggle Item
            hk_flags = MF_STRING | (MF_CHECKED if snapshot.hotkey_running else MF_UNCHECKED)
            hk_label = f"Hotkey: Enabled ({snapshot.hotkey_combination})" if snapshot.hotkey_running else "Hotkey: Disabled"
            self._backend.append_menu_item(hmenu, hk_flags, IDM_TOGGLE_HOTKEY, hk_label)

            # 3. Status Submenu
            h_sub = self._backend.create_popup_menu()
            if h_sub:
                ctrl_str = "Assistant: Ready" if snapshot.controller_running else "Assistant: Stopped"
                self._backend.append_menu_item(h_sub, MF_STRING | MF_GRAYED, IDM_STATUS_ASSISTANT, ctrl_str)

                hk_str = f"Hotkey: Active ({snapshot.hotkey_combination})" if snapshot.hotkey_running else f"Hotkey: {snapshot.hotkey_state}"
                self._backend.append_menu_item(h_sub, MF_STRING | MF_GRAYED, IDM_STATUS_HOTKEY, hk_str)

                voice_str = "Voice: Active (recording)" if snapshot.voice_active else "Voice: Idle"
                self._backend.append_menu_item(h_sub, MF_STRING | MF_GRAYED, IDM_STATUS_HEADER, voice_str)

                speech_str = "Speech: Available (Push-to-Talk)" if snapshot.speech_available else "Speech: Unavailable"
                self._backend.append_menu_item(h_sub, MF_STRING | MF_GRAYED, IDM_STATUS_SPEECH, speech_str)

                ai_str = f"AI: Enabled ({snapshot.ai_profile})" if snapshot.ai_enabled else "AI: Disabled"
                self._backend.append_menu_item(h_sub, MF_STRING | MF_GRAYED, IDM_STATUS_AI, ai_str)

                self._backend.append_submenu(hmenu, MF_STRING, h_sub, "Status")

            self._backend.append_separator(hmenu)

            # 4. Exit Anaya
            self._backend.append_menu_item(hmenu, MF_STRING, IDM_EXIT, "Exit Anaya")

            # Win32 KB131501 popup menu focus fix
            self._backend.set_foreground_window(hwnd)
            self._backend.track_popup_menu(hmenu, x, y, hwnd)
            self._backend.post_message(hwnd, WM_NULL, 0, 0)

        finally:
            self._backend.destroy_menu(hmenu)

    def _handle_primary_action(self) -> None:
        """Handle primary left-click or keyboard selection on tray icon."""
        if self._on_open is not None:
            try:
                self._on_open()
            except Exception as exc:
                logger.debug("Error in on_open callback: %s", exc)

    def _handle_menu_command(self, cmd_id: int) -> None:
        """Handle incoming WM_COMMAND from context menu."""
        if cmd_id == IDM_OPEN:
            self._handle_primary_action()
        elif cmd_id == IDM_TOGGLE_HOTKEY:
            self._request_hotkey_toggle()
        elif cmd_id == IDM_EXIT:
            if self._on_exit is not None:
                try:
                    self._on_exit()
                except Exception as exc:
                    logger.debug("Error in on_exit callback: %s", exc)

    def _handle_refresh(self, hwnd: int) -> None:
        """Update tooltip and UI state when refreshed by background worker."""
        snapshot = capture_status_snapshot(self._controller, self._hotkey_service)
        status_text = "Hotkey: Active" if snapshot.hotkey_running else "Hotkey: Inactive"
        new_tip = f"Anaya AI Assistant ({status_text})"
        self.update_tooltip(new_tip)
