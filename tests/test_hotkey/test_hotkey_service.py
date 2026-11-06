"""Unit tests for GlobalHotkeyService verifying lifecycle, dispatch, concurrency, and shutdown."""

from __future__ import annotations

import logging
import math
import sys
import threading
from threading import Event
from typing import Any

import pytest

from app.hotkey.models import (
    HotkeyCombination,
    HotkeyServiceState,
    WM_HOTKEY,
    WM_QUIT,
)
from app.hotkey.service import GlobalHotkeyService
from app.speech.models import PipelineResult


class MockMsg:
    """Mock structure mimicking Win32 MSG."""

    def __init__(self, message: int = 0, wParam: int = 0, lParam: int = 0) -> None:
        self.hwnd = None
        self.message = message
        self.wParam = wParam
        self.lParam = lParam
        self.time = 0
        self.pt = None


class MockHotkeyBackend:
    """Thread-safe mock backend simulating Win32 message queue and hotkey APIs."""

    def __init__(self) -> None:
        self.is_available_flag = True
        self.queue_initialized = False
        self.registered_hotkeys: list[tuple[int, int, int]] = []
        self.unregistered_hotkeys: list[int] = []
        self.unregister_thread_ids: list[int] = []
        self.last_error_code = 0
        self.should_fail_registration = False
        self.should_fail_post_quit = False
        self.should_fail_unregistration = False
        self.should_raise_on_unregister = False
        self.block_registration_event: Event | None = None
        self.registration_attempted_event = Event()

        # Thread communication for GetMessage
        self._msg_queue: list[MockMsg] = []
        self._queue_lock = threading.Lock()
        self._queue_condition = threading.Condition(self._queue_lock)
        self._quit_received = False
        self._thread_id = 12345

    def is_available(self) -> bool:
        return self.is_available_flag

    def initialize_message_queue(self) -> None:
        self.queue_initialized = True

    def get_current_thread_id(self) -> int:
        return self._thread_id

    def register_hotkey(self, hotkey_id: int, fs_modifiers: int, vk_code: int) -> bool:
        self.registration_attempted_event.set()
        if self.block_registration_event is not None:
            self.block_registration_event.wait()

        if self.should_fail_registration:
            return False

        self.registered_hotkeys.append((hotkey_id, fs_modifiers, vk_code))
        return True

    def unregister_hotkey(self, hotkey_id: int) -> bool:
        self.unregister_thread_ids.append(threading.get_ident())
        if self.should_raise_on_unregister:
            raise RuntimeError("Win32 hardware fault during unregistration.")
        if self.should_fail_unregistration:
            return False
        self.unregistered_hotkeys.append(hotkey_id)
        return True

    def get_last_error(self) -> int:
        return self.last_error_code

    def create_msg(self) -> MockMsg:
        return MockMsg()

    def get_message(self, msg: Any) -> int:
        with self._queue_condition:
            while not self._msg_queue and not self._quit_received:
                self._queue_condition.wait(timeout=0.1)

            if self._quit_received:
                msg.message = WM_QUIT
                msg.wParam = 0
                return 0

            if self._msg_queue:
                incoming = self._msg_queue.pop(0)
                msg.message = incoming.message
                msg.wParam = incoming.wParam
                msg.lParam = incoming.lParam
                return 1

            return 1

    def post_thread_quit(self, thread_id: int) -> bool:
        if self.should_fail_post_quit:
            return False

        with self._queue_condition:
            self._quit_received = True
            self._queue_condition.notify_all()
        return True

    def extract_msg_details(self, msg: Any) -> tuple[int, int]:
        return int(msg.message), int(msg.wParam)

    def inject_hotkey_event(self, hotkey_id: int = 1) -> None:
        """Helper to inject a synthetic WM_HOTKEY message into the queue."""
        with self._queue_condition:
            self._msg_queue.append(MockMsg(message=WM_HOTKEY, wParam=hotkey_id))
            self._queue_condition.notify_all()


class MockAssistantController:
    """Mock controller capturing handle_voice invocations."""

    def __init__(self, failure_error: Exception | None = None) -> None:
        self.handle_voice_calls: list[dict[str, Any]] = []
        self.failure_error = failure_error
        self.block_voice_event: Event | None = None
        self.voice_invoked_event = Event()

    def handle_voice(self, duration: float | None = None, speak_response: bool = False) -> PipelineResult:
        self.handle_voice_calls.append({"duration": duration, "speak_response": speak_response})
        self.voice_invoked_event.set()

        if self.block_voice_event is not None:
            self.block_voice_event.wait()

        if self.failure_error is not None:
            raise self.failure_error

        return PipelineResult(
            transcript="test command",
            action_result=None,
            spoken_response=speak_response,
            success=True,
        )


class TestGlobalHotkeyServiceLifecycle:
    """Test suite for lifecycle state machine and Win32 thread ownership."""

    def test_clean_start_stop_lifecycle(self) -> None:
        """Verify normal STOPPED -> STARTING -> RUNNING -> STOPPING -> STOPPED transitions."""
        backend = MockHotkeyBackend()
        ctrl = MockAssistantController()
        service = GlobalHotkeyService(controller=ctrl, backend=backend)

        assert service.state == HotkeyServiceState.STOPPED
        assert service.is_running is False
        assert service.is_cleanly_stopped is True

        # Start service
        started = service.start(startup_timeout=2.0)
        assert started is True
        assert service.state == HotkeyServiceState.RUNNING
        assert service.is_running is True
        assert backend.queue_initialized is True
        assert len(backend.registered_hotkeys) == 1

        # Stop service
        service.stop(timeout=1.0)
        assert service.state == HotkeyServiceState.STOPPED
        assert service.is_running is False
        assert service.is_cleanly_stopped is True
        assert service.is_worker_cleanly_terminated is True
        assert len(backend.unregistered_hotkeys) == 1

    def test_pump_unregisters_on_its_own_thread(self) -> None:
        """Verify unregistration executes strictly on the message-pump thread in its finally block."""
        backend = MockHotkeyBackend()
        ctrl = MockAssistantController()
        service = GlobalHotkeyService(controller=ctrl, backend=backend)

        service.start(startup_timeout=2.0)
        service.stop(timeout=1.0)

        assert len(backend.unregister_thread_ids) == 1
        # The unregister thread ID must NOT be the main/caller thread ID
        assert backend.unregister_thread_ids[0] != threading.get_ident()

    def test_registration_conflict_1409_handling(self) -> None:
        """Verify error 1409 transitions state to REGISTRATION_FAILED and spawns no voice worker."""
        backend = MockHotkeyBackend()
        backend.should_fail_registration = True
        backend.last_error_code = 1409

        ctrl = MockAssistantController()
        service = GlobalHotkeyService(controller=ctrl, backend=backend)

        started = service.start(startup_timeout=2.0)
        assert started is False
        assert service.state == HotkeyServiceState.REGISTRATION_FAILED
        assert service.is_running is False
        assert service.is_worker_alive is False

        # Clean shutdown after failed registration
        service.stop(timeout=1.0)

    def test_stop_during_registration(self) -> None:
        """Verify initiating stop while STARTING safely unregisters and exits without hanging."""
        backend = MockHotkeyBackend()
        backend.block_registration_event = Event()

        ctrl = MockAssistantController()
        service = GlobalHotkeyService(controller=ctrl, backend=backend)

        # Start service in background
        start_thread = threading.Thread(target=service.start, kwargs={"startup_timeout": 3.0})
        start_thread.start()

        # Wait until registration is attempted
        backend.registration_attempted_event.wait(timeout=2.0)
        assert service.state == HotkeyServiceState.STARTING

        # Initiate stop while registration is still blocked
        backend.block_registration_event.set()
        service.stop(timeout=2.0)
        start_thread.join(timeout=2.0)

        assert service.state == HotkeyServiceState.STOPPED
        assert service.is_cleanly_stopped is True


class TestGlobalHotkeyServiceDispatch:
    """Test suite for hotkey trigger handling and atomic voice gate."""

    def test_synthetic_hotkey_dispatches_voice_task(self) -> None:
        """Verify WM_HOTKEY dispatches to controller.handle_voice with duration and speak_response=True."""
        backend = MockHotkeyBackend()
        ctrl = MockAssistantController()
        received_results: list[PipelineResult] = []

        service = GlobalHotkeyService(
            controller=ctrl,
            duration=5.0,
            backend=backend,
            on_voice_result=received_results.append,
        )

        try:
            service.start(startup_timeout=2.0)

            backend.inject_hotkey_event(hotkey_id=1)

            # Wait for controller invocation
            called = ctrl.voice_invoked_event.wait(timeout=2.0)
            assert called is True
            assert len(ctrl.handle_voice_calls) == 1
            assert ctrl.handle_voice_calls[0] == {"duration": 5.0, "speak_response": True}
        finally:
            service.stop(timeout=1.0)

    def test_atomic_voice_gate_drops_repeated_presses(self) -> None:
        """Verify that while voice processing is active, subsequent hotkey presses are dropped immediately."""
        backend = MockHotkeyBackend()
        ctrl = MockAssistantController()
        ctrl.block_voice_event = Event()  # Hold worker active

        service = GlobalHotkeyService(
            controller=ctrl,
            duration=5.0,
            backend=backend,
        )

        try:
            service.start(startup_timeout=2.0)

            # First hotkey trigger
            backend.inject_hotkey_event(hotkey_id=1)
            ctrl.voice_invoked_event.wait(timeout=2.0)

            assert service.is_voice_active is True

            # Fire 3 more presses while busy
            backend.inject_hotkey_event(hotkey_id=1)
            backend.inject_hotkey_event(hotkey_id=1)
            backend.inject_hotkey_event(hotkey_id=1)

            # Ensure message pump processed the queued messages while voice interaction was still blocked
            for _ in range(50):
                if len(backend._msg_queue) == 0:
                    break
                threading.Event().wait(0.01)

            # Unblock first interaction
            ctrl.block_voice_event.set()

            # Wait for voice interaction to complete
            for _ in range(50):
                if not service.is_voice_active:
                    break
                threading.Event().wait(0.02)

            assert service.is_voice_active is False
            # Exactly one invocation occurred; all 3 subsequent presses were dropped
            assert len(ctrl.handle_voice_calls) == 1

        finally:
            if ctrl.block_voice_event is not None:
                ctrl.block_voice_event.set()
            service.stop(timeout=1.0)

    def test_worker_exception_does_not_crash_service(self) -> None:
        """Verify unhandled controller error is caught and logged, releasing busy gate."""
        backend = MockHotkeyBackend()
        ctrl = MockAssistantController(failure_error=RuntimeError("Audio hardware fault."))

        service = GlobalHotkeyService(
            controller=ctrl,
            backend=backend,
        )

        try:
            service.start(startup_timeout=2.0)

            backend.inject_hotkey_event(hotkey_id=1)
            ctrl.voice_invoked_event.wait(timeout=2.0)

            # Wait for busy gate release
            for _ in range(50):
                if not service.is_voice_active:
                    break
                threading.Event().wait(0.02)

            assert service.is_voice_active is False
            assert service.is_running is True
        finally:
            service.stop(timeout=1.0)


class TestGlobalHotkeyServiceShutdownAndBudget:
    """Test suite for timeout budget, retained thread references, and stop retry."""

    def test_worker_timeout_tracks_hung_worker_and_prevents_clean_shutdown(self) -> None:
        """Verify that when worker join times out, thread reference is retained and clean shutdown is denied."""
        backend = MockHotkeyBackend()
        ctrl = MockAssistantController()
        ctrl.block_voice_event = Event()

        service = GlobalHotkeyService(controller=ctrl, backend=backend)
        service.start(startup_timeout=2.0)

        # Trigger voice task and wait for it to enter execution
        backend.inject_hotkey_event(hotkey_id=1)
        ctrl.voice_invoked_event.wait(timeout=2.0)
        assert service.is_voice_active is True

        # Stop with very short timeout while worker is blocked
        service.stop(timeout=0.05)

        # Must report TIMEOUT_STOPPED and retain thread reference
        assert service.state == HotkeyServiceState.TIMEOUT_STOPPED
        assert service.is_voice_active is True
        assert service.is_worker_alive is True
        assert service.is_worker_cleanly_terminated is False
        assert service.is_cleanly_stopped is False

        # Unblock worker and retry stop
        ctrl.block_voice_event.set()

        # Retry stop with sufficient timeout
        service.stop(timeout=1.0)

        assert service.state == HotkeyServiceState.STOPPED
        assert service.is_voice_active is False
        assert service.is_worker_alive is False
        assert service.is_worker_cleanly_terminated is True
        assert service.is_cleanly_stopped is True

    def test_failed_post_thread_quit_handling(self, caplog: pytest.LogCaptureFixture) -> None:
        """Verify handling when PostThreadMessageW fails: retains reference and logs warning."""
        backend = MockHotkeyBackend()
        backend.should_fail_post_quit = True

        ctrl = MockAssistantController()
        service = GlobalHotkeyService(controller=ctrl, backend=backend)

        service.start(startup_timeout=2.0)

        with caplog.at_level(logging.WARNING):
            service.stop(timeout=0.05)

        assert "Failed to post WM_QUIT" in caplog.text
        assert service.state == HotkeyServiceState.TIMEOUT_STOPPED
        assert service.is_cleanly_stopped is False

        # Clean up pump thread to avoid leak
        backend.should_fail_post_quit = False
        backend.post_thread_quit(backend.get_current_thread_id())
        service.stop(timeout=1.0)
        assert service.is_cleanly_stopped is True

    def test_zero_and_small_timeout_budget_enforcement(self) -> None:
        """Verify zero timeout executes non-blocking checks without unbounded waits."""
        backend = MockHotkeyBackend()
        ctrl = MockAssistantController()
        service = GlobalHotkeyService(controller=ctrl, backend=backend)

        service.start(startup_timeout=2.0)

        # Zero timeout must not raise and must not block indefinitely
        service.stop(timeout=0.0)

    def test_zero_and_small_timeout_budget_when_post_quit_fails(self) -> None:
        """Verify zero or small timeout does not exceed budget even when post_thread_quit repeatedly fails."""
        import time

        backend = MockHotkeyBackend()
        backend.should_fail_post_quit = True

        ctrl = MockAssistantController()
        service = GlobalHotkeyService(controller=ctrl, backend=backend)

        service.start(startup_timeout=2.0)

        # Zero timeout with failing quit must complete without sleep overrun (< 50ms)
        t0 = time.monotonic()
        service.stop(timeout=0.0)
        elapsed = time.monotonic() - t0

        assert elapsed < 0.05
        assert service.state == HotkeyServiceState.TIMEOUT_STOPPED
        assert service.is_cleanly_stopped is False

        # Clean up pump
        backend.should_fail_post_quit = False
        backend.post_thread_quit(backend.get_current_thread_id())
        service.stop(timeout=1.0)
        assert service.is_cleanly_stopped is True

    def test_reject_invalid_and_negative_timeouts(self) -> None:
        """Verify stop() rejects negative, boolean, and non-finite timeouts before side-effects."""
        backend = MockHotkeyBackend()
        ctrl = MockAssistantController()
        service = GlobalHotkeyService(controller=ctrl, backend=backend)

        # Negative timeout
        with pytest.raises(ValueError, match="non-negative finite number"):
            service.stop(timeout=-1.0)

        # Boolean timeout
        with pytest.raises(TypeError, match="non-negative number"):
            service.stop(timeout=True)  # type: ignore[arg-type]

        # Non-finite timeout
        with pytest.raises(ValueError, match="non-negative finite number"):
            service.stop(timeout=math.inf)

        with pytest.raises(ValueError, match="non-negative finite number"):
            service.stop(timeout=math.nan)


class TestCrossPlatformImportSafety:
    """Test suite verifying safe behavior on non-Windows platforms."""

    def test_non_windows_platform_graceful_degradation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify that on non-Windows platforms, service safely reports unavailable and disabled."""
        monkeypatch.setattr(sys, "platform", "linux")

        # Win32HotkeyBackend should report unavailable without error
        from app.hotkey.backend import Win32HotkeyBackend

        backend = Win32HotkeyBackend()
        assert backend.is_available() is False

        ctrl = MockAssistantController()
        service = GlobalHotkeyService(controller=ctrl, backend=backend)

        # start() must return False and transition to REGISTRATION_FAILED without raising
        started = service.start()
        assert started is False
        assert service.state == HotkeyServiceState.REGISTRATION_FAILED
        assert service.is_running is False


class TestStartupTimeoutAndLateRegistration:
    """Test suite verifying startup timeout and late registration abort."""

    def test_startup_timeout_prevents_late_registration_transition_to_running(self) -> None:
        """Verify:
        1. Registration deliberately blocks beyond startup_timeout.
        2. start() returns failure and transitions to PUMP_FAILED.
        3. Registration is subsequently released.
        4. The pump exits and unregisters on its own thread.
        5. The service never transitions to RUNNING.
        6. No voice worker is started.
        7. Clean shutdown is reported only after confirmed cleanup.
        """
        backend = MockHotkeyBackend()
        backend.block_registration_event = Event()

        ctrl = MockAssistantController()
        service = GlobalHotkeyService(controller=ctrl, backend=backend)

        # 1 & 2: start() with short timeout while registration is blocked
        started = service.start(startup_timeout=0.05)
        assert started is False
        assert service.state == HotkeyServiceState.PUMP_FAILED
        assert service.is_running is False
        assert service.is_worker_alive is False
        assert service.is_worker_cleanly_terminated is True

        pump_thread = service._pump_thread
        assert pump_thread is not None
        assert pump_thread.is_alive() is True

        # 3: Release registration
        backend.block_registration_event.set()

        # 4 & 5: Wait for pump thread to terminate
        pump_thread.join(timeout=2.0)
        assert pump_thread.is_alive() is False

        # Service never transitioned to RUNNING
        assert service.state == HotkeyServiceState.PUMP_FAILED
        assert service.is_running is False

        # 6: No voice worker was started
        assert service.is_worker_alive is False
        assert service.is_worker_cleanly_terminated is True
        assert len(ctrl.handle_voice_calls) == 0

        # Unregistered on its own thread
        assert len(backend.unregistered_hotkeys) == 1
        assert backend.unregister_thread_ids[0] == pump_thread.ident

        # 7: Clean stop cleanup
        service.stop(timeout=1.0)
        assert service.state == HotkeyServiceState.STOPPED
        assert service.is_cleanly_stopped is True

    def test_startup_timeout_racing_with_registration_completion_and_running_publication(self) -> None:
        """Verify:
        1. Pump completes registration and acquires lock right as wait() times out.
        2. Pump attempts to publish RUNNING.
        3. start() unconditionally cancels startup, forces state to PUMP_FAILED, and wakes pump.
        4. start() returns False, state is never left in RUNNING, and no voice worker is created.
        5. Successful registration is unregistered on the pump thread.
        6. Pump eventually terminates.
        """
        backend = MockHotkeyBackend()
        registration_gate = Event()
        backend.block_registration_event = registration_gate

        ctrl = MockAssistantController()
        service = GlobalHotkeyService(controller=ctrl, backend=backend)

        # Hook wait() to release registration and let pump acquire lock right before wait returns False
        def wait_with_race_release(timeout: float | None = None) -> bool:
            registration_gate.set()
            # Wait until pump thread has executed register_hotkey and published RUNNING
            for _ in range(50):
                with service._lock:
                    if service.state == HotkeyServiceState.RUNNING:
                        break
                threading.Event().wait(0.005)
            return False

        service._startup_event.wait = wait_with_race_release  # type: ignore[method-assign]

        started = service.start(startup_timeout=0.01)

        # Must return False and must NOT remain in RUNNING
        assert started is False
        assert service.state == HotkeyServiceState.PUMP_FAILED
        assert service.is_running is False
        assert service._worker_thread is None
        assert service.is_worker_alive is False

        # Verify pump thread terminates
        assert service._pump_thread is not None
        service._pump_thread.join(timeout=2.0)
        assert service._pump_thread.is_alive() is False

        # Unregistration occurred on pump thread
        assert len(backend.unregistered_hotkeys) == 1
        assert backend.unregister_thread_ids[0] == service._pump_thread.ident

        # Clean shutdown after cleanup
        service.stop(timeout=1.0)
        assert service.state == HotkeyServiceState.STOPPED
        assert service.is_cleanly_stopped is True


class TestUnsafeRestartPrevention:
    """Test suite verifying rejection of unsafe restart attempts."""

    def test_reject_start_while_stopping(self) -> None:
        """Verify start() returns False and does not spawn new threads when state is STOPPING."""
        backend = MockHotkeyBackend()
        ctrl = MockAssistantController()
        service = GlobalHotkeyService(controller=ctrl, backend=backend)

        with service._lock:
            service._state = HotkeyServiceState.STOPPING

        assert service.start(startup_timeout=1.0) is False
        assert service._pump_thread is None

    def test_reject_start_while_worker_alive_after_timeout(self) -> None:
        """Verify start() is rejected when previous worker thread is alive after TIMEOUT_STOPPED."""
        backend = MockHotkeyBackend()
        ctrl = MockAssistantController()
        ctrl.block_voice_event = Event()

        service = GlobalHotkeyService(controller=ctrl, backend=backend)
        service.start(startup_timeout=2.0)

        # Trigger voice and hold it
        backend.inject_hotkey_event(1)
        ctrl.voice_invoked_event.wait(timeout=2.0)

        # Timed-out stop retains worker
        service.stop(timeout=0.05)
        assert service.state == HotkeyServiceState.TIMEOUT_STOPPED
        assert service.is_worker_alive is True

        orig_worker = service._worker_thread
        orig_pump = service._pump_thread

        # start() must be rejected
        assert service.start(startup_timeout=1.0) is False

        # Original thread references remain intact
        assert service._worker_thread is orig_worker
        assert service._pump_thread is orig_pump

        # Clean up
        ctrl.block_voice_event.set()
        service.stop(timeout=2.0)
        assert service.is_cleanly_stopped is True

    def test_reject_start_while_pump_thread_alive(self) -> None:
        """Verify start() is rejected when previous pump thread remains alive."""
        backend = MockHotkeyBackend()
        backend.block_registration_event = Event()

        ctrl = MockAssistantController()
        service = GlobalHotkeyService(controller=ctrl, backend=backend)

        # Start with timeout
        service.start(startup_timeout=0.05)
        assert service.state == HotkeyServiceState.PUMP_FAILED
        orig_pump = service._pump_thread
        assert orig_pump is not None
        assert orig_pump.is_alive() is True

        # start() must be rejected while pump is alive
        assert service.start(startup_timeout=1.0) is False
        assert service._pump_thread is orig_pump

        # Clean up
        backend.block_registration_event.set()
        orig_pump.join(timeout=2.0)
        service.stop(timeout=1.0)
        assert service.is_cleanly_stopped is True

    def test_reject_start_from_pump_failed_without_cleanup(self) -> None:
        """Verify that a service in PUMP_FAILED rejects start() until clean stop cleanup occurs."""
        backend = MockHotkeyBackend()
        backend.block_registration_event = Event()

        ctrl = MockAssistantController()
        service = GlobalHotkeyService(controller=ctrl, backend=backend)

        # 1. Cause PUMP_FAILED via timeout
        started = service.start(startup_timeout=0.05)
        assert started is False
        assert service.state == HotkeyServiceState.PUMP_FAILED

        # Release pump registration and wait for pump thread to terminate
        backend.block_registration_event.set()
        assert service._pump_thread is not None
        service._pump_thread.join(timeout=2.0)
        assert service._pump_thread.is_alive() is False

        # 2. Direct restart from PUMP_FAILED must be rejected even though pump thread is dead
        assert service.state == HotkeyServiceState.PUMP_FAILED
        assert service.start(startup_timeout=1.0) is False

        # 3. Explicit stop() performs cleanup and transitions to STOPPED
        service.stop(timeout=1.0)
        assert service.state == HotkeyServiceState.STOPPED
        assert service.is_cleanly_stopped is True

        # 4. Now that clean stop occurred, start() is permitted
        started_after_stop = service.start(startup_timeout=2.0)
        assert started_after_stop is True
        assert service.state == HotkeyServiceState.RUNNING

        service.stop(timeout=1.0)
        assert service.is_cleanly_stopped is True

    def test_clean_restart_lifecycle_after_clean_stop(self) -> None:
        """Verify service supports clean restart cycles when stop() terminates cleanly."""
        backend = MockHotkeyBackend()
        ctrl = MockAssistantController()
        service = GlobalHotkeyService(controller=ctrl, backend=backend)

        # Cycle 1
        assert service.start(startup_timeout=2.0) is True
        assert service.state == HotkeyServiceState.RUNNING
        service.stop(timeout=1.0)
        assert service.state == HotkeyServiceState.STOPPED
        assert service.is_cleanly_stopped is True

        # Cycle 2
        assert service.start(startup_timeout=2.0) is True
        assert service.state == HotkeyServiceState.RUNNING
        service.stop(timeout=1.0)
        assert service.state == HotkeyServiceState.STOPPED
        assert service.is_cleanly_stopped is True

    def test_concurrent_start_calls_cannot_create_duplicate_threads(self) -> None:
        """Verify multiple threads calling start() simultaneously results in exactly one runner and no duplicate threads."""
        backend = MockHotkeyBackend()
        ctrl = MockAssistantController()
        service = GlobalHotkeyService(controller=ctrl, backend=backend)

        results: list[bool] = []
        barrier = threading.Barrier(5)

        def runner() -> None:
            barrier.wait()
            res = service.start(startup_timeout=2.0)
            results.append(res)

        threads = [threading.Thread(target=runner) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # At least one succeeded, all returned cleanly, exactly 1 pump and 1 worker created
        assert any(results) is True
        assert service.state == HotkeyServiceState.RUNNING
        assert service._pump_thread is not None
        assert service._worker_thread is not None
        assert len(backend.registered_hotkeys) == 1

        service.stop(timeout=1.0)
        assert service.is_cleanly_stopped is True

    def test_concurrent_stop_calls_are_safe_and_deterministic(self) -> None:
        """Verify multiple threads calling stop() concurrently safely converge to STOPPED."""
        backend = MockHotkeyBackend()
        ctrl = MockAssistantController()
        service = GlobalHotkeyService(controller=ctrl, backend=backend)

        service.start(startup_timeout=2.0)
        assert service.state == HotkeyServiceState.RUNNING

        barrier = threading.Barrier(5)

        def stop_runner() -> None:
            barrier.wait()
            service.stop(timeout=1.0)

        threads = [threading.Thread(target=stop_runner) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert service.state == HotkeyServiceState.STOPPED
        assert service.is_cleanly_stopped is True
        assert service._pump_thread is None
        assert service._worker_thread is None


class TestUnregistrationFailureTracking:
    """Test suite verifying tracking of Win32 unregistration failures."""

    def test_unregistration_returning_false_denies_clean_stop(self) -> None:
        """Verify that when unregister_hotkey returns False, is_cleanly_stopped is False."""
        backend = MockHotkeyBackend()
        backend.should_fail_unregistration = True
        backend.last_error_code = 1400

        ctrl = MockAssistantController()
        service = GlobalHotkeyService(controller=ctrl, backend=backend)

        service.start(startup_timeout=2.0)
        service.stop(timeout=1.0)

        assert service.state == HotkeyServiceState.TIMEOUT_STOPPED
        assert service.is_cleanly_stopped is False
        assert service._unregistration_failed is True
        # Unregistration was attempted on the pump thread
        assert len(backend.unregister_thread_ids) == 1
        assert backend.unregister_thread_ids[0] != threading.get_ident()

    def test_unregistration_raising_exception_denies_clean_stop(self) -> None:
        """Verify that when unregister_hotkey raises, exception is trapped and clean stop is denied."""
        backend = MockHotkeyBackend()
        backend.should_raise_on_unregister = True

        ctrl = MockAssistantController()
        service = GlobalHotkeyService(controller=ctrl, backend=backend)

        service.start(startup_timeout=2.0)
        service.stop(timeout=1.0)

        assert service.state == HotkeyServiceState.TIMEOUT_STOPPED
        assert service.is_cleanly_stopped is False
        assert service._unregistration_failed is True
        assert len(backend.unregister_thread_ids) == 1
        assert backend.unregister_thread_ids[0] != threading.get_ident()

    def test_repeated_stop_after_unregistration_failure_retains_timeout_stopped(self) -> None:
        """Verify calling stop() again after unregistration failed never reports STOPPED or clean shutdown."""
        backend = MockHotkeyBackend()
        backend.should_fail_unregistration = True

        ctrl = MockAssistantController()
        service = GlobalHotkeyService(controller=ctrl, backend=backend)

        service.start(startup_timeout=2.0)
        service.stop(timeout=1.0)

        assert service.state == HotkeyServiceState.TIMEOUT_STOPPED
        assert service.is_cleanly_stopped is False

        # Second stop call
        service.stop(timeout=1.0)
        assert service.state == HotkeyServiceState.TIMEOUT_STOPPED
        assert service.is_cleanly_stopped is False

        # Attempting start() must also be rejected
        assert service.start(startup_timeout=1.0) is False


class TestStartupTimeoutValidation:
    """Test suite verifying defensive validation of startup_timeout in start()."""

    @pytest.mark.parametrize(
        "invalid_timeout, expected_exc",
        [
            (True, ValueError),
            (False, ValueError),
            ("3.0", ValueError),
            (None, ValueError),
            (-1.0, ValueError),
            (-0.001, ValueError),
            (math.nan, ValueError),
            (math.inf, ValueError),
            (-math.inf, ValueError),
        ],
    )
    def test_reject_invalid_startup_timeouts(self, invalid_timeout: Any, expected_exc: type[Exception]) -> None:
        """Verify start() raises ValueError for invalid startup_timeout before side effects."""
        backend = MockHotkeyBackend()
        ctrl = MockAssistantController()
        service = GlobalHotkeyService(controller=ctrl, backend=backend)

        with pytest.raises(expected_exc):
            service.start(startup_timeout=invalid_timeout)

        # No threads created, state unchanged
        assert service.state == HotkeyServiceState.STOPPED
        assert service._pump_thread is None
        assert service._worker_thread is None
