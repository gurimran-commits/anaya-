"""Lifecycle tests for DesktopApplicationRunner."""

from __future__ import annotations

import math
import signal
import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.core.config import AppConfig
from app.desktop.runner import DesktopApplicationRunner, RunnerState
from app.hotkey.models import HotkeyServiceState
from app.hud.models import HudServiceState
from app.main import main, parse_args
from app.tray.models import TrayServiceState


class MockController:
    """Mock controller tracking lifecycle calls and timestamps matching real AssistantController contract."""

    def __init__(
        self,
        start_success: bool = True,
        stop_success: bool = True,
        start_exception: Exception | None = None,
        stop_exception: Exception | None = None,
    ) -> None:
        self.start_success = start_success
        self.stop_success = stop_success
        self.start_exception = start_exception
        self.stop_exception = stop_exception

        self.start_called = False
        self.stop_called = False
        self.start_time: float | None = None
        self.stop_time: float | None = None
        self.received_timeout: float | None = None

        self._is_running = False
        self._is_cleanly_stopped = False

    @property
    def is_running(self) -> bool:
        return self._is_running

    @is_running.setter
    def is_running(self, val: bool) -> None:
        self._is_running = val

    @property
    def is_cleanly_stopped(self) -> bool:
        return self._is_cleanly_stopped

    @is_cleanly_stopped.setter
    def is_cleanly_stopped(self, val: bool) -> None:
        self._is_cleanly_stopped = val

    def start(self) -> None:
        self.start_called = True
        self.start_time = time.monotonic()
        if self.start_exception is not None:
            raise self.start_exception
        if self.start_success:
            self._is_running = True
        else:
            self._is_running = False
        return None

    def stop(self, timeout: float = 5.0) -> None:
        self.stop_called = True
        self.stop_time = time.monotonic()
        self.received_timeout = timeout
        if self.stop_exception is not None:
            raise self.stop_exception
        self._is_running = False
        if self.stop_success:
            self._is_cleanly_stopped = True
        else:
            self._is_cleanly_stopped = False
        return None


class MockHudService:
    """Mock HUD service tracking lifecycle and worker states."""

    def __init__(self, start_return: bool = True, stop_return: bool = True) -> None:
        self.start_return = start_return
        self.stop_return = stop_return
        self.start_called = False
        self.stop_called = False
        self.start_time: float | None = None
        self.stop_time: float | None = None
        self.show_called = False
        self.hide_called = False
        self.worker_alive = False
        self.gui_alive = False
        self.cleanly_stopped = False
        self.state = HudServiceState.STOPPED
        self.received_timeout: float | None = None

    @property
    def is_worker_alive(self) -> bool:
        return self.worker_alive

    @property
    def is_gui_alive(self) -> bool:
        return self.gui_alive

    @property
    def is_cleanly_stopped(self) -> bool:
        return self.cleanly_stopped

    def start(self) -> bool:
        self.start_called = True
        self.start_time = time.monotonic()
        if self.start_return:
            self.state = HudServiceState.RUNNING
            self.gui_alive = True
            return True
        self.state = HudServiceState.ERROR
        return False

    def stop(self, timeout: float = 3.0) -> bool:
        self.stop_called = True
        self.stop_time = time.monotonic()
        self.received_timeout = timeout
        if self.stop_return:
            self.state = HudServiceState.STOPPED
            self.gui_alive = False
            self.cleanly_stopped = not self.worker_alive
            return True
        return False

    def show(self) -> None:
        self.show_called = True

    def hide(self) -> None:
        self.hide_called = True


class MockHotkeyService:
    """Mock hotkey service tracking lifecycle and coordinator injection."""

    def __init__(self, start_return: bool = True) -> None:
        self.start_return = start_return
        self.start_called = False
        self.stop_called = False
        self.shutdown_called = False
        self.start_time: float | None = None
        self.shutdown_time: float | None = None
        self.worker_alive = False
        self.cleanly_stopped = False
        self.state = HotkeyServiceState.STOPPED
        self.received_timeout: float | None = None

    @property
    def is_worker_alive(self) -> bool:
        return self.worker_alive

    @property
    def is_cleanly_stopped(self) -> bool:
        return self.cleanly_stopped

    def start(self, startup_timeout: float = 3.0) -> bool:
        self.start_called = True
        self.start_time = time.monotonic()
        if self.start_return:
            self.state = HotkeyServiceState.RUNNING
            return True
        self.state = HotkeyServiceState.REGISTRATION_FAILED
        return False

    def stop(self, timeout: float = 2.0) -> None:
        self.stop_called = True
        self.state = HotkeyServiceState.STOPPED

    def shutdown(self, timeout: float = 2.0) -> None:
        self.shutdown_called = True
        self.shutdown_time = time.monotonic()
        self.received_timeout = timeout
        if not self.worker_alive:
            self.state = HotkeyServiceState.STOPPED
            self.cleanly_stopped = True
        else:
            self.state = HotkeyServiceState.TIMEOUT_STOPPED
            self.cleanly_stopped = False


class MockTrayService:
    """Mock tray service tracking lifecycle."""

    def __init__(self, start_return: bool = True) -> None:
        self.start_return = start_return
        self.start_called = False
        self.stop_called = False
        self.start_time: float | None = None
        self.stop_time: float | None = None
        self.cleanly_stopped = False
        self.state = TrayServiceState.STOPPED
        self.received_timeout: float | None = None

    @property
    def is_cleanly_stopped(self) -> bool:
        return self.cleanly_stopped

    def start(self) -> bool:
        self.start_called = True
        self.start_time = time.monotonic()
        if self.start_return:
            self.state = TrayServiceState.RUNNING
            return True
        self.state = TrayServiceState.ERROR
        return False

    def stop(self, timeout: float = 2.0) -> None:
        self.stop_called = True
        self.stop_time = time.monotonic()
        self.received_timeout = timeout
        self.state = TrayServiceState.STOPPED
        self.cleanly_stopped = True


class MockNotificationHandler:
    """Mock queued notification handler tracking lifecycle."""

    def __init__(self, start_success: bool = True, stop_success: bool = True) -> None:
        self.start_success = start_success
        self.stop_success = stop_success
        self.start_called = False
        self.stop_called = False
        self.start_time: float | None = None
        self.stop_time: float | None = None
        self.is_running = False
        self.received_timeout: float | None = None

    def start(self) -> None:
        self.start_called = True
        self.start_time = time.monotonic()
        if self.start_success:
            self.is_running = True

    def stop(self, timeout: float = 2.0) -> None:
        self.stop_called = True
        self.stop_time = time.monotonic()
        self.received_timeout = timeout
        if self.stop_success:
            self.is_running = False


def test_runner_initial_state() -> None:
    """Initial state must be STOPPED, not running, and not cleanly stopped."""
    runner = DesktopApplicationRunner()
    assert runner.state == RunnerState.STOPPED
    assert not runner.is_running
    assert not runner.is_cleanly_stopped
    assert runner.controller is None
    assert runner.hud_service is None
    assert runner.hotkey_service is None
    assert runner.tray_service is None


def test_runner_config_validation() -> None:
    """Runner validates desktop configuration types and non-empty AUMID."""
    mock_bad_cfg = MagicMock()
    mock_bad_cfg.desktop_enabled = "not_a_bool"
    with pytest.raises(TypeError, match="desktop_enabled must be a bool"):
        DesktopApplicationRunner(config=mock_bad_cfg)

    mock_bad_cfg2 = MagicMock()
    mock_bad_cfg2.desktop_enabled = True
    mock_bad_cfg2.desktop_tray_enabled = True
    mock_bad_cfg2.desktop_hud_enabled = True
    mock_bad_cfg2.desktop_hud_start_hidden = True
    mock_bad_cfg2.desktop_notifications_enabled = True
    mock_bad_cfg2.desktop_aumid = "   "
    with pytest.raises(ValueError, match="desktop_aumid must be a non-empty string"):
        DesktopApplicationRunner(config=mock_bad_cfg2)


def test_successful_startup_and_shutdown() -> None:
    """Runner starts all subsystems and shuts them down cleanly."""
    controller = MockController()
    hud = MockHudService()
    hotkey = MockHotkeyService()
    tray = MockTrayService()
    notif = MockNotificationHandler()

    config = AppConfig(desktop_enabled=True, hotkey_enabled=True)
    runner = DesktopApplicationRunner(
        config=config,
        controller=controller,  # type: ignore[arg-type]
        hud_service=hud,  # type: ignore[arg-type]
        hotkey_service=hotkey,  # type: ignore[arg-type]
        tray_service=tray,  # type: ignore[arg-type]
        notification_handler=notif,  # type: ignore[arg-type]
    )

    started = runner.start()
    assert started is True
    assert runner.is_running
    assert runner.state == RunnerState.RUNNING

    assert controller.start_called
    assert hud.start_called
    assert hotkey.start_called
    assert tray.start_called
    assert notif.start_called

    stopped = runner.stop(timeout=5.0)
    assert stopped is True
    assert runner.state == RunnerState.STOPPED
    assert runner.is_cleanly_stopped

    assert tray.stop_called
    assert hotkey.shutdown_called
    assert hud.stop_called
    assert controller.stop_called
    assert notif.stop_called


def test_initialization_ordering() -> None:
    """Subsystems must initialize in exact dependency order: notification -> controller -> hud -> hotkey -> tray."""
    call_log: list[str] = []

    controller = MockController()
    def _ctrl_start() -> None:
        call_log.append("controller")
        controller.is_running = True
    controller.start = _ctrl_start  # type: ignore[assignment]

    hud = MockHudService()
    hud.start = lambda: call_log.append("hud") or True  # type: ignore[assignment]

    hotkey = MockHotkeyService()
    hotkey.start = lambda **kw: call_log.append("hotkey") or True  # type: ignore[assignment]

    tray = MockTrayService()
    tray.start = lambda: call_log.append("tray") or True  # type: ignore[assignment]

    notif = MockNotificationHandler()
    notif.start = lambda: call_log.append("notif") or True  # type: ignore[assignment]

    config = AppConfig(desktop_enabled=True, hotkey_enabled=True)
    runner = DesktopApplicationRunner(
        config=config,
        controller=controller,  # type: ignore[arg-type]
        hud_service=hud,  # type: ignore[arg-type]
        hotkey_service=hotkey,  # type: ignore[arg-type]
        tray_service=tray,  # type: ignore[arg-type]
        notification_handler=notif,  # type: ignore[arg-type]
    )

    started = runner.start()
    assert started is True
    assert call_log == ["notif", "controller", "hud", "hotkey", "tray"]


def test_reverse_shutdown_ordering() -> None:
    """Subsystems must terminate in reverse order: tray -> hotkey -> hud -> controller -> notif."""
    call_log: list[str] = []

    controller = MockController()
    def _ctrl_stop(timeout: float = 0.0) -> None:
        call_log.append("controller")
        controller.is_running = False
        controller.is_cleanly_stopped = True
    controller.stop = _ctrl_stop  # type: ignore[assignment]

    hud = MockHudService()
    def _hud_stop(timeout=0.0):
        call_log.append("hud")
        hud.gui_alive = False
        hud.cleanly_stopped = True
        hud.state = HudServiceState.STOPPED
        return True
    hud.stop = _hud_stop  # type: ignore[assignment]

    hotkey = MockHotkeyService()
    def _hk_shutdown(timeout=0.0):
        call_log.append("hotkey")
        hotkey.state = HotkeyServiceState.STOPPED
        hotkey.cleanly_stopped = True
    hotkey.shutdown = _hk_shutdown  # type: ignore[assignment]

    tray = MockTrayService()
    def _tray_stop(timeout=0.0):
        call_log.append("tray")
        tray.state = TrayServiceState.STOPPED
        tray.cleanly_stopped = True
    tray.stop = _tray_stop  # type: ignore[assignment]

    notif = MockNotificationHandler()
    def _notif_stop(timeout=0.0):
        call_log.append("notif")
        notif.is_running = False
    notif.stop = _notif_stop  # type: ignore[assignment]

    config = AppConfig(desktop_enabled=True, hotkey_enabled=True)
    runner = DesktopApplicationRunner(
        config=config,
        controller=controller,  # type: ignore[arg-type]
        hud_service=hud,  # type: ignore[arg-type]
        hotkey_service=hotkey,  # type: ignore[arg-type]
        tray_service=tray,  # type: ignore[arg-type]
        notification_handler=notif,  # type: ignore[arg-type]
    )

    assert runner.start() is True
    call_log.clear()

    assert runner.stop() is True
    assert call_log == ["tray", "hotkey", "hud", "controller", "notif"]


def test_controller_start_failure_rollback() -> None:
    """When controller fails to start, initialized resources are rolled back and runner enters ERROR."""
    controller = MockController(start_success=False)
    notif = MockNotificationHandler()

    runner = DesktopApplicationRunner(
        controller=controller,  # type: ignore[arg-type]
        notification_handler=notif,  # type: ignore[arg-type]
    )

    started = runner.start()
    assert started is False
    assert runner.state == RunnerState.ERROR
    assert notif.stop_called


def test_hud_start_failure_degraded_mode() -> None:
    """When HUD fails to start but Tray starts successfully, runner continues in degraded mode."""
    controller = MockController()
    hud = MockHudService(start_return=False)
    tray = MockTrayService(start_return=True)

    config = AppConfig(desktop_enabled=True, desktop_hud_enabled=True, desktop_tray_enabled=True, hotkey_enabled=False)
    runner = DesktopApplicationRunner(
        config=config,
        controller=controller,  # type: ignore[arg-type]
        hud_service=hud,  # type: ignore[arg-type]
        tray_service=tray,  # type: ignore[arg-type]
    )

    started = runner.start()
    assert started is True
    assert runner.is_running
    assert runner.state == RunnerState.RUNNING


def test_both_hud_and_tray_fail_fatal() -> None:
    """When both HUD and Tray are unavailable, runner rolls back and enters ERROR."""
    controller = MockController()
    hud = MockHudService(start_return=False)
    tray = MockTrayService(start_return=False)
    notif = MockNotificationHandler()

    config = AppConfig(desktop_enabled=True, desktop_hud_enabled=True, desktop_tray_enabled=True, hotkey_enabled=False)
    runner = DesktopApplicationRunner(
        config=config,
        controller=controller,  # type: ignore[arg-type]
        hud_service=hud,  # type: ignore[arg-type]
        tray_service=tray,  # type: ignore[arg-type]
        notification_handler=notif,  # type: ignore[arg-type]
    )

    started = runner.start()
    assert started is False
    assert runner.state == RunnerState.ERROR
    assert controller.stop_called
    assert notif.stop_called


def test_tray_fail_ensures_hud_visible() -> None:
    """When Tray fails to start but HUD is running, HUD is forced visible so user has an accessible UI."""
    controller = MockController()
    hud = MockHudService(start_return=True)
    tray = MockTrayService(start_return=False)

    config = AppConfig(
        desktop_enabled=True,
        desktop_hud_enabled=True,
        desktop_hud_start_hidden=True,
        desktop_tray_enabled=True,
        hotkey_enabled=False,
    )
    runner = DesktopApplicationRunner(
        config=config,
        controller=controller,  # type: ignore[arg-type]
        hud_service=hud,  # type: ignore[arg-type]
        tray_service=tray,  # type: ignore[arg-type]
    )

    started = runner.start()
    assert started is True
    assert runner.is_running
    assert hud.show_called


def test_hotkey_start_conflict_degraded_mode() -> None:
    """When hotkey fails to register (e.g. conflict), runner warns and continues without hotkey."""
    controller = MockController()
    hud = MockHudService(start_return=True)
    hotkey = MockHotkeyService(start_return=False)

    config = AppConfig(desktop_enabled=True, hotkey_enabled=True)
    runner = DesktopApplicationRunner(
        config=config,
        controller=controller,  # type: ignore[arg-type]
        hud_service=hud,  # type: ignore[arg-type]
        hotkey_service=hotkey,  # type: ignore[arg-type]
    )

    started = runner.start()
    assert started is True
    assert runner.is_running
    assert hotkey.state == HotkeyServiceState.REGISTRATION_FAILED


def test_shared_monotonic_deadline() -> None:
    """Shutdown budget must decrease monotonically across service stops without resetting."""
    controller = MockController()
    hud = MockHudService()
    hotkey = MockHotkeyService()
    tray = MockTrayService()
    notif = MockNotificationHandler()

    # Artificially consume 0.05s in tray stop
    def slow_tray_stop(timeout: float):
        time.sleep(0.05)
        tray.received_timeout = timeout
        tray.state = TrayServiceState.STOPPED
        tray.cleanly_stopped = True
    tray.stop = slow_tray_stop  # type: ignore[assignment]

    config = AppConfig(desktop_enabled=True, hotkey_enabled=True)
    runner = DesktopApplicationRunner(
        config=config,
        controller=controller,  # type: ignore[arg-type]
        hud_service=hud,  # type: ignore[arg-type]
        hotkey_service=hotkey,  # type: ignore[arg-type]
        tray_service=tray,  # type: ignore[arg-type]
        notification_handler=notif,  # type: ignore[arg-type]
    )

    runner.start()
    total_timeout = 2.0
    runner.stop(timeout=total_timeout)

    assert tray.received_timeout is not None
    assert hotkey.received_timeout is not None
    assert hud.received_timeout is not None
    assert notif.received_timeout is not None

    # Each subsequent service must receive strictly less than the starting budget
    assert tray.received_timeout <= total_timeout
    assert hotkey.received_timeout < tray.received_timeout
    assert hud.received_timeout <= hotkey.received_timeout
    assert notif.received_timeout <= hud.received_timeout


def test_controller_shutdown_deferred_while_worker_alive() -> None:
    """When a HUD or hotkey worker is alive, controller.stop() is deferred to protect SQLite."""
    controller = MockController()
    hud = MockHudService()
    hud.worker_alive = True  # Worker is active

    config = AppConfig(desktop_enabled=True, hotkey_enabled=False)
    runner = DesktopApplicationRunner(
        config=config,
        controller=controller,  # type: ignore[arg-type]
        hud_service=hud,  # type: ignore[arg-type]
    )

    runner.start()
    stopped = runner.stop(timeout=1.0)

    assert stopped is False
    assert runner.state == RunnerState.STOPPING
    assert not runner.is_cleanly_stopped
    # CRITICAL: Controller must NOT be stopped while worker is alive
    assert not controller.stop_called


def test_stop_retry_after_worker_terminates() -> None:
    """Calling stop() again after the worker terminates cleanly completes shutdown."""
    controller = MockController()
    hud = MockHudService()
    hud.worker_alive = True

    config = AppConfig(desktop_enabled=True, hotkey_enabled=False)
    runner = DesktopApplicationRunner(
        config=config,
        controller=controller,  # type: ignore[arg-type]
        hud_service=hud,  # type: ignore[arg-type]
    )

    runner.start()
    first_stop = runner.stop(timeout=1.0)
    assert first_stop is False
    assert not controller.stop_called

    # Worker now terminates
    hud.worker_alive = False
    hud.cleanly_stopped = True

    # Retry stop
    retry_stop = runner.stop(timeout=1.0)
    assert retry_stop is True
    assert controller.stop_called
    assert runner.state == RunnerState.STOPPED
    assert runner.is_cleanly_stopped


def test_single_use_runner_lifecycle() -> None:
    """A runner instance cannot be started a second time after stopping or initial start."""
    controller = MockController()
    hud = MockHudService()

    runner = DesktopApplicationRunner(
        controller=controller,  # type: ignore[arg-type]
        hud_service=hud,  # type: ignore[arg-type]
    )

    assert runner.start() is True
    assert runner.stop() is True

    # Second start rejected
    assert runner.start() is False


def test_cli_desktop_routing() -> None:
    """The --desktop flag correctly routes to DesktopApplicationRunner.run()."""
    parsed = parse_args(["--desktop"])
    assert parsed.desktop is True

    with patch("app.desktop.runner.DesktopApplicationRunner.run", return_value=0) as mock_run:
        code = main(argv=["--desktop"])
        assert code == 0
        assert mock_run.called


def test_controller_returns_none_startup_success() -> None:
    """Controller start() returning None and setting is_running=True allows desktop startup to succeed."""
    controller = MockController(start_success=True)
    hud = MockHudService(start_return=True)

    config = AppConfig(desktop_enabled=True, desktop_tray_enabled=False, hotkey_enabled=False)
    runner = DesktopApplicationRunner(
        config=config,
        controller=controller,  # type: ignore[arg-type]
        hud_service=hud,  # type: ignore[arg-type]
    )

    started = runner.start()
    assert started is True
    assert runner.is_running
    assert controller.start_called
    assert controller.is_running
    assert runner.state == RunnerState.RUNNING


def test_controller_start_failure_not_running_rollback() -> None:
    """Controller start() completing without error but leaving is_running=False triggers rollback and ERROR."""
    controller = MockController(start_success=False)
    notif = MockNotificationHandler()

    runner = DesktopApplicationRunner(
        controller=controller,  # type: ignore[arg-type]
        notification_handler=notif,  # type: ignore[arg-type]
    )

    started = runner.start()
    assert started is False
    assert runner.state == RunnerState.ERROR
    assert notif.stop_called


def test_controller_start_exception_rollback() -> None:
    """Controller start() raising an exception triggers rollback and enters ERROR."""
    controller = MockController(start_exception=RuntimeError("Simulated controller start exception"))
    notif = MockNotificationHandler()

    runner = DesktopApplicationRunner(
        controller=controller,  # type: ignore[arg-type]
        notification_handler=notif,  # type: ignore[arg-type]
    )

    started = runner.start()
    assert started is False
    assert runner.state == RunnerState.ERROR
    assert notif.stop_called


def test_controller_stop_returns_none_clean_shutdown() -> None:
    """Controller stop() returning None and setting is_cleanly_stopped=True is treated as clean shutdown."""
    controller = MockController(start_success=True, stop_success=True)
    hud = MockHudService(start_return=True, stop_return=True)

    config = AppConfig(desktop_enabled=True, desktop_tray_enabled=False, hotkey_enabled=False)
    runner = DesktopApplicationRunner(
        config=config,
        controller=controller,  # type: ignore[arg-type]
        hud_service=hud,  # type: ignore[arg-type]
    )

    assert runner.start() is True
    stopped = runner.stop(timeout=2.0)
    assert stopped is True
    assert runner.state == RunnerState.STOPPED
    assert runner.is_cleanly_stopped
    assert controller.stop_called
    assert controller.is_cleanly_stopped
    assert controller.received_timeout is not None


def test_controller_stop_incomplete_shutdown_detected() -> None:
    """Controller stop() completing with is_cleanly_stopped=False marks runner stop incomplete."""
    controller = MockController(start_success=True, stop_success=False)
    hud = MockHudService(start_return=True, stop_return=True)

    config = AppConfig(desktop_enabled=True, desktop_tray_enabled=False, hotkey_enabled=False)
    runner = DesktopApplicationRunner(
        config=config,
        controller=controller,  # type: ignore[arg-type]
        hud_service=hud,  # type: ignore[arg-type]
    )

    assert runner.start() is True
    stopped = runner.stop(timeout=2.0)
    assert stopped is False
    assert not runner.is_cleanly_stopped
    assert runner.state == RunnerState.STOPPING
    assert controller.stop_called
    assert not controller.is_cleanly_stopped


def test_controller_stop_exception_handling() -> None:
    """Controller stop() raising an exception is caught and marked as incomplete shutdown."""
    controller = MockController(start_success=True, stop_exception=RuntimeError("Controller close failed"))
    hud = MockHudService(start_return=True, stop_return=True)

    config = AppConfig(desktop_enabled=True, desktop_tray_enabled=False, hotkey_enabled=False)
    runner = DesktopApplicationRunner(
        config=config,
        controller=controller,  # type: ignore[arg-type]
        hud_service=hud,  # type: ignore[arg-type]
    )

    assert runner.start() is True
    stopped = runner.stop(timeout=2.0)
    assert stopped is False
    assert not runner.is_cleanly_stopped
    assert runner.state == RunnerState.STOPPING
    assert controller.stop_called
