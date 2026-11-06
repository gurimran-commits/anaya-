"""Desktop application lifecycle runner integrating Anaya Windows subsystems."""

from __future__ import annotations

from enum import Enum
import logging
import math
import signal
import sys
import threading
import time
from typing import TYPE_CHECKING, Any

from app.bootstrap import create_controller
from app.core.config import DEFAULT_CONFIG, AppConfig
from app.hotkey.models import HotkeyServiceState
from app.hotkey.service import GlobalHotkeyService
from app.hud.coordinator import VoiceSessionCoordinator
from app.hud.models import HudServiceState
from app.hud.service import FloatingHudService
from app.notifications.queued_handler import QueuedNotificationHandler
from app.notifications.windows_toast import WindowsToastNotificationHandler
from app.tray.models import TrayServiceState
from app.tray.service import WindowsTrayService

if TYPE_CHECKING:
    from app.controller import AssistantController

logger = logging.getLogger(__name__)


class RunnerState(str, Enum):
    """Lifecycle states of the DesktopApplicationRunner."""

    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    ERROR = "error"


class DesktopApplicationRunner:
    """Lifecycle orchestrator integrating desktop subsystems under a unified runtime.

    Subsystems:
        - AssistantController (authoritative controller)
        - VoiceSessionCoordinator (single shared microphone lease manager)
        - QueuedNotificationHandler (asynchronous notification delivery)
        - FloatingHudService (presentation HUD window and input surface)
        - GlobalHotkeyService (Win32 background hotkey listener)
        - WindowsTrayService (system tray icon and contextual menu)
    """

    def __init__(
        self,
        config: AppConfig | None = None,
        controller: AssistantController | None = None,
        coordinator: VoiceSessionCoordinator | None = None,
        hud_service: FloatingHudService | None = None,
        hotkey_service: GlobalHotkeyService | None = None,
        tray_service: WindowsTrayService | None = None,
        notification_handler: QueuedNotificationHandler | None = None,
    ) -> None:
        self._config = config or DEFAULT_CONFIG
        self._validate_config(self._config)

        self._controller = controller
        self._coordinator = coordinator
        self._hud_service = hud_service
        self._hotkey_service = hotkey_service
        self._tray_service = tray_service
        self._notification_handler = notification_handler

        self._lock = threading.RLock()
        self._state = RunnerState.STOPPED
        self._has_started = False
        self._stop_requested = False
        self._stop_event = threading.Event()
        self._is_cleanly_stopped = False

    @staticmethod
    def _validate_config(cfg: AppConfig) -> None:
        """Defensively validate desktop-specific configuration attributes."""
        if not isinstance(cfg.desktop_enabled, bool):
            raise TypeError(f"desktop_enabled must be a bool, got {type(cfg.desktop_enabled).__name__}")
        if not isinstance(cfg.desktop_tray_enabled, bool):
            raise TypeError(f"desktop_tray_enabled must be a bool, got {type(cfg.desktop_tray_enabled).__name__}")
        if not isinstance(cfg.desktop_hud_enabled, bool):
            raise TypeError(f"desktop_hud_enabled must be a bool, got {type(cfg.desktop_hud_enabled).__name__}")
        if not isinstance(cfg.desktop_hud_start_hidden, bool):
            raise TypeError(f"desktop_hud_start_hidden must be a bool, got {type(cfg.desktop_hud_start_hidden).__name__}")
        if not isinstance(cfg.desktop_notifications_enabled, bool):
            raise TypeError(f"desktop_notifications_enabled must be a bool, got {type(cfg.desktop_notifications_enabled).__name__}")
        if not isinstance(cfg.desktop_aumid, str) or not cfg.desktop_aumid.strip():
            raise ValueError(f"desktop_aumid must be a non-empty string, got {cfg.desktop_aumid!r}")

    @property
    def config(self) -> AppConfig:
        """Current application configuration."""
        return self._config

    @property
    def state(self) -> RunnerState:
        """Current lifecycle state of the runner."""
        with self._lock:
            return self._state

    @property
    def is_running(self) -> bool:
        """True if the runner has completed startup and is actively running."""
        with self._lock:
            return self._state == RunnerState.RUNNING

    @property
    def is_cleanly_stopped(self) -> bool:
        """True if all managed subsystems terminated cleanly within shutdown budgets."""
        with self._lock:
            return self._is_cleanly_stopped

    @property
    def controller(self) -> AssistantController | None:
        """Managed AssistantController instance."""
        return self._controller

    @property
    def coordinator(self) -> VoiceSessionCoordinator | None:
        """Shared VoiceSessionCoordinator instance."""
        return self._coordinator

    @property
    def hud_service(self) -> FloatingHudService | None:
        """Managed FloatingHudService instance."""
        return self._hud_service

    @property
    def hotkey_service(self) -> GlobalHotkeyService | None:
        """Managed GlobalHotkeyService instance."""
        return self._hotkey_service

    @property
    def tray_service(self) -> WindowsTrayService | None:
        """Managed WindowsTrayService instance."""
        return self._tray_service

    @property
    def notification_handler(self) -> QueuedNotificationHandler | None:
        """Managed QueuedNotificationHandler instance."""
        return self._notification_handler

    def request_stop(self) -> None:
        """Thread-safe signal to request application shutdown."""
        self._stop_requested = True
        self._stop_event.set()

    def start(self) -> bool:
        """Start desktop services in dependency order with rollback on failure.

        Returns:
            True if desktop application is running (fully or in approved degraded mode).
            False if fatal initialization failure occurred.
        """
        with self._lock:
            if self._has_started:
                logger.warning("DesktopApplicationRunner cannot be restarted.")
                return False
            self._has_started = True
            self._state = RunnerState.STARTING

        # 1. Voice Session Coordinator
        if self._coordinator is None:
            self._coordinator = VoiceSessionCoordinator()

        # 2. Notification Handler
        if self._notification_handler is None and self._config.desktop_notifications_enabled:
            toast_handler = WindowsToastNotificationHandler(aumid=self._config.desktop_aumid)
            self._notification_handler = QueuedNotificationHandler(target_handler=toast_handler)

        if self._notification_handler is not None:
            try:
                self._notification_handler.start()
                if not self._notification_handler.is_running:
                    logger.warning("QueuedNotificationHandler failed to start; continuing without notifications.")
                    self._notification_handler = None
            except Exception as exc:
                logger.warning("Error starting QueuedNotificationHandler: %s", exc)
                self._notification_handler = None

        # 3. Controller
        if self._controller is None:
            try:
                self._controller = create_controller(
                    config=self._config,
                    custom_notification_handler=self._notification_handler,
                )
            except Exception as exc:
                logger.error("Failed to create AssistantController: %s", exc, exc_info=True)
                self._rollback_startup()
                with self._lock:
                    self._state = RunnerState.ERROR
                return False

        try:
            self._controller.start()
            if not self._controller.is_running:
                logger.error("AssistantController failed to start (is_running is False).")
                self._rollback_startup()
                with self._lock:
                    self._state = RunnerState.ERROR
                return False
        except Exception as exc:
            logger.error("Failed to start AssistantController: %s", exc, exc_info=True)
            self._rollback_startup()
            with self._lock:
                self._state = RunnerState.ERROR
            return False

        # 4. Floating HUD Service
        hud_usable = False
        if self._config.desktop_hud_enabled:
            if self._hud_service is None:
                self._hud_service = FloatingHudService(
                    controller=self._controller,
                    coordinator=self._coordinator,
                )
            try:
                hud_started = self._hud_service.start()
                if hud_started:
                    hud_usable = True
                    if self._config.desktop_hud_start_hidden:
                        self._hud_service.hide()
                    else:
                        self._hud_service.show()
                else:
                    logger.warning("FloatingHudService failed to start.")
            except Exception as exc:
                logger.warning("Error starting FloatingHudService: %s", exc)

        # 5. Global Hotkey Service
        if self._config.hotkey_enabled:
            if self._hotkey_service is None:
                self._hotkey_service = GlobalHotkeyService(
                    controller=self._controller,
                    combination=self._config.hotkey_combination,
                    duration=self._config.hotkey_voice_duration,
                    coordinator=self._coordinator,
                )
            try:
                hk_started = self._hotkey_service.start()
                if not hk_started:
                    logger.warning("GlobalHotkeyService failed to start; continuing without hotkey.")
            except Exception as exc:
                logger.warning("Error starting GlobalHotkeyService: %s", exc)

        # 6. Windows Tray Service
        tray_usable = False
        if self._config.desktop_tray_enabled:
            if self._tray_service is None:
                on_open_cb = self._hud_service.show if hud_usable and self._hud_service is not None else None
                self._tray_service = WindowsTrayService(
                    controller=self._controller,
                    hotkey_service=self._hotkey_service,
                    on_open=on_open_cb,
                    on_exit=self.request_stop,
                )

            try:
                tray_started = self._tray_service.start()
                if tray_started:
                    tray_usable = True
                else:
                    logger.warning("WindowsTrayService failed to start.")
            except Exception as exc:
                logger.warning("Error starting WindowsTrayService: %s", exc)

        # 7. Usability Evaluation and Degraded Mode Policy
        # Both HUD and Tray unavailable -> Fatal
        if not hud_usable and not tray_usable:
            logger.error("Neither HUD nor Tray is available. Cannot provide a desktop user interface.")
            self._rollback_startup()
            with self._lock:
                self._state = RunnerState.ERROR
            return False

        # If tray unavailable, but HUD is usable: ensure HUD is visible
        if not tray_usable and hud_usable and self._hud_service is not None:
            logger.info("Tray is unavailable; ensuring HUD is visible.")
            self._hud_service.show()

        with self._lock:
            self._state = RunnerState.RUNNING
            return True

    def _rollback_startup(self) -> None:
        """Roll back partially initialized services during startup failure."""
        logger.info("Rolling back partially initialized desktop services...")
        deadline = time.monotonic() + 5.0

        if self._tray_service is not None:
            try:
                rem = max(0.0, deadline - time.monotonic())
                self._tray_service.stop(timeout=rem)
            except Exception as exc:
                logger.debug("Error rolling back tray service: %s", exc)

        if self._hotkey_service is not None:
            try:
                rem = max(0.0, deadline - time.monotonic())
                self._hotkey_service.shutdown(timeout=rem)
            except Exception as exc:
                logger.debug("Error rolling back hotkey service: %s", exc)

        if self._hud_service is not None:
            try:
                rem = max(0.0, deadline - time.monotonic())
                self._hud_service.stop(timeout=rem)
            except Exception as exc:
                logger.debug("Error rolling back HUD service: %s", exc)

        if self._controller is not None:
            try:
                rem = max(0.0, deadline - time.monotonic())
                self._controller.stop(timeout=rem)
            except Exception as exc:
                logger.debug("Error rolling back controller: %s", exc)

        if self._notification_handler is not None:
            try:
                rem = max(0.0, deadline - time.monotonic())
                self._notification_handler.stop(timeout=rem)
            except Exception as exc:
                logger.debug("Error rolling back notification handler: %s", exc)

    def stop(self, timeout: float = 10.0) -> bool:
        """Stop all desktop subsystems in reverse dependency order within a single monotonic deadline.

        Args:
            timeout: Maximum total seconds allowed for the entire shutdown sequence.

        Returns:
            True if all active subsystems cleanly terminated within the deadline.
            False if any thread remains alive or shutdown was incomplete.
        """
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise TypeError(f"timeout must be a non-negative number, got {type(timeout).__name__}")
        if not math.isfinite(timeout) or timeout < 0.0:
            raise ValueError(f"timeout must be a non-negative finite number, got {timeout}")

        deadline = time.monotonic() + float(timeout)

        with self._lock:
            self._stop_requested = True
            self._stop_event.set()
            if self._state == RunnerState.STOPPED and self._is_cleanly_stopped:
                return True
            self._state = RunnerState.STOPPING

        all_clean = True

        # 1. Stop Tray Service
        if self._tray_service is not None:
            remaining = max(0.0, deadline - time.monotonic())
            self._tray_service.stop(timeout=remaining)
            if self._tray_service.state != TrayServiceState.STOPPED or not self._tray_service.is_cleanly_stopped:
                all_clean = False
                logger.warning("Tray service did not stop cleanly.")

        # 2. Permanently Shut Down Hotkey Service
        if self._hotkey_service is not None:
            remaining = max(0.0, deadline - time.monotonic())
            self._hotkey_service.shutdown(timeout=remaining)
            if self._hotkey_service.state != HotkeyServiceState.STOPPED or not self._hotkey_service.is_cleanly_stopped:
                all_clean = False
                logger.warning("Hotkey service did not stop cleanly.")

        # 3. Stop HUD Service
        if self._hud_service is not None:
            remaining = max(0.0, deadline - time.monotonic())
            hud_ok = self._hud_service.stop(timeout=remaining)
            if not hud_ok or not self._hud_service.is_cleanly_stopped:
                all_clean = False
                logger.warning("HUD service did not stop cleanly.")

        # 4. Critical database safety invariant: check if any controller-using worker is alive
        hud_worker_alive = self._hud_service is not None and self._hud_service.is_worker_alive
        hotkey_worker_alive = self._hotkey_service is not None and self._hotkey_service.is_worker_alive

        if hud_worker_alive or hotkey_worker_alive:
            logger.warning(
                "Controller shutdown DEFERRED: worker thread is still alive (hud_worker=%s, hotkey_worker=%s). "
                "Retaining component references to protect SQLite database.",
                hud_worker_alive,
                hotkey_worker_alive,
            )
            all_clean = False
            # Do NOT stop controller or close database connections!
        else:
            # 5. Stop Controller
            if self._controller is not None:
                remaining = max(0.0, deadline - time.monotonic())
                try:
                    self._controller.stop(timeout=remaining)
                    if not self._controller.is_cleanly_stopped:
                        all_clean = False
                        logger.warning("AssistantController stop was incomplete.")
                except Exception as exc:
                    all_clean = False
                    logger.error("Error stopping AssistantController: %s", exc, exc_info=True)

        # 6. Stop Queued Notification Handler
        if self._notification_handler is not None:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                self._notification_handler.stop(timeout=remaining)
                if self._notification_handler.is_running:
                    all_clean = False
                    logger.warning("Notification handler worker did not stop within timeout budget.")
            except Exception as exc:
                all_clean = False
                logger.error("Error stopping notification handler: %s", exc)

        with self._lock:
            any_worker_alive = (
                (self._hud_service is not None and (self._hud_service.is_worker_alive or self._hud_service.is_gui_alive))
                or (self._hotkey_service is not None and (self._hotkey_service.is_worker_alive or not self._hotkey_service.is_cleanly_stopped))
                or (self._tray_service is not None and not self._tray_service.is_cleanly_stopped)
            )
            if any_worker_alive or not all_clean:
                self._is_cleanly_stopped = False
                self._state = RunnerState.STOPPING
                return False

            self._is_cleanly_stopped = True
            self._state = RunnerState.STOPPED
            return True

    def run(self) -> int:
        """Run the desktop application lifecycle on the current thread.

        Returns:
            0 on clean execution and shutdown.
            1 on startup error or incomplete shutdown.
        """
        if not self.start():
            return 1

        orig_handlers: dict[int, Any] = {}
        can_install_signals = False
        try:
            if threading.current_thread() is threading.main_thread():
                can_install_signals = True
                signals_to_handle = [signal.SIGINT, signal.SIGTERM]
                if hasattr(signal, "SIGBREAK"):
                    signals_to_handle.append(signal.SIGBREAK)

                def _sig_handler(signum: int, frame: Any) -> None:
                    logger.info("Received signal %s; requesting desktop shutdown.", signum)
                    self.request_stop()

                for sig in signals_to_handle:
                    try:
                        orig_handlers[sig] = signal.signal(sig, _sig_handler)
                    except (ValueError, OSError) as exc:
                        logger.debug("Could not register signal %s: %s", sig, exc)
        except Exception as exc:
            logger.debug("Signal installation error: %s", exc)

        try:
            self._stop_event.wait()
        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt received in runner; requesting shutdown.")
            self.request_stop()
        finally:
            if can_install_signals:
                for sig, handler in orig_handlers.items():
                    try:
                        signal.signal(sig, handler)
                    except Exception:
                        pass

        clean = self.stop()
        return 0 if clean else 1
