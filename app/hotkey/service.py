"""Global hotkey service orchestrating Win32 message pump and voice worker."""

from __future__ import annotations

import logging
import math
import sys
import time
from threading import Event, Lock, RLock, Thread
from typing import TYPE_CHECKING, Any, Callable

from app.hotkey.backend import HotkeyBackend, Win32HotkeyBackend
from app.hotkey.models import (
    HotkeyCombination,
    HotkeyConfigurationError,
    HotkeyServiceState,
    WM_HOTKEY,
)

if TYPE_CHECKING:
    from app.speech.models import PipelineResult

logger = logging.getLogger(__name__)


class GlobalHotkeyService:
    """Windows global hotkey service triggering bounded push-to-talk voice interactions.

    Guarantees:
        - Win32 thread ownership: The dedicated message-pump thread exclusively initializes
          its message queue, registers the hotkey, processes WM_HOTKEY, and unregisters in finally.
        - Single dedicated voice worker: Uses exactly one non-daemon (daemon=False) worker thread.
        - Zero unbounded queues: Hotkey presses while voice processing is in-flight are dropped immediately.
        - Controller safety: Serializes voice interactions; controller resources remain valid while active.
        - Monotonic bounded shutdown: stop(timeout) enforces a single monotonic deadline across joins.
    """

    def __init__(
        self,
        controller: Any,
        combination: HotkeyCombination | str | None = None,
        duration: float = 5.0,
        backend: HotkeyBackend | None = None,
        hotkey_id: int = 1,
        on_voice_result: Callable[[PipelineResult], None] | None = None,
    ) -> None:
        self._controller = controller

        # Validate duration
        if isinstance(duration, bool) or not isinstance(duration, (int, float)):
            raise TypeError(f"duration must be a float or int, got {type(duration).__name__}")
        if not math.isfinite(duration) or duration <= 0.0 or duration > 60.0:
            raise ValueError(f"duration must be positive and at most 60.0s, got {duration}")
        self._duration = float(duration)

        # Parse combination
        if combination is None:
            self._combination = HotkeyCombination.parse("win+alt+a")
        elif isinstance(combination, str):
            self._combination = HotkeyCombination.parse(combination)
        elif isinstance(combination, HotkeyCombination):
            self._combination = combination
        else:
            raise HotkeyConfigurationError(
                f"Invalid combination type: expected HotkeyCombination or str, got {type(combination).__name__}"
            )

        # Injected backend
        self._backend = backend or Win32HotkeyBackend()
        self._hotkey_id = int(hotkey_id)
        self._on_voice_result = on_voice_result

        # Lifecycle synchronization
        self._lock = RLock()
        self._state = HotkeyServiceState.STOPPED
        self._stop_requested = False
        self._unregistration_failed = False

        self._startup_event = Event()
        self._pump_stopped_event = Event()

        self._pump_thread: Thread | None = None
        self._pump_thread_id: int | None = None

        # Dedicated single non-daemon voice worker
        self._worker_thread: Thread | None = None
        self._worker_stop_event = Event()
        self._work_trigger_event = Event()
        self._voice_busy = False
        self._voice_lock = Lock()

    @property
    def combination(self) -> HotkeyCombination:
        """Configured shortcut combination."""
        return self._combination

    @property
    def duration(self) -> float:
        """Bounded push-to-talk recording duration in seconds."""
        return self._duration

    @property
    def state(self) -> HotkeyServiceState:
        """Current lifecycle state."""
        with self._lock:
            return self._state

    @property
    def is_running(self) -> bool:
        """True if the service is actively registered and listening for hotkey presses."""
        with self._lock:
            return self._state == HotkeyServiceState.RUNNING

    @property
    def is_voice_active(self) -> bool:
        """True strictly while a voice interaction is currently in progress."""
        with self._voice_lock:
            return self._voice_busy

    @property
    def is_worker_alive(self) -> bool:
        """True while the voice-worker thread is alive."""
        with self._lock:
            return self._worker_thread is not None and self._worker_thread.is_alive()

    @property
    def is_worker_cleanly_terminated(self) -> bool:
        """True when the voice worker thread has cleanly terminated."""
        with self._lock:
            return self._worker_thread is None

    @property
    def is_cleanly_stopped(self) -> bool:
        """True only when pump and worker threads have both cleanly terminated and registration is inactive."""
        with self._lock:
            return (
                self._state == HotkeyServiceState.STOPPED
                and self._pump_thread is None
                and self._worker_thread is None
                and not self._unregistration_failed
            )

    def start(self, startup_timeout: float = 3.0) -> bool:
        """Start the message pump and voice worker threads.

        Returns:
            True if registration succeeded and service is RUNNING.
            False if registration failed, timed out, or platform is unsupported.
        """
        # Defensive validation of startup_timeout
        if isinstance(startup_timeout, bool) or not isinstance(startup_timeout, (int, float)):
            raise ValueError(f"startup_timeout must be a non-negative number, got {type(startup_timeout).__name__}")
        if not math.isfinite(startup_timeout) or startup_timeout < 0.0:
            raise ValueError(f"startup_timeout must be a non-negative finite number, got {startup_timeout}")

        with self._lock:
            if self._state == HotkeyServiceState.RUNNING:
                return True
            if self._state != HotkeyServiceState.STOPPED:
                return False

            # Reject start if previous threads are still alive or unregistration failed
            if (self._pump_thread is not None and self._pump_thread.is_alive()) or self.is_worker_alive:
                return False
            if self._unregistration_failed:
                return False

            if not self._backend.is_available():
                logger.info(
                    "Global hotkey backend is unavailable on platform '%s'. Hotkey service disabled.",
                    sys.platform,
                )
                self._state = HotkeyServiceState.REGISTRATION_FAILED
                return False

            # Clean reset of session state for a fresh start
            self._state = HotkeyServiceState.STARTING
            self._stop_requested = False
            self._unregistration_failed = False
            self._pump_thread = None
            self._pump_thread_id = None
            self._worker_thread = None
            self._startup_event.clear()
            self._pump_stopped_event.clear()
            self._worker_stop_event.clear()
            self._work_trigger_event.clear()
            with self._voice_lock:
                self._voice_busy = False

            # Start message-pump thread first to own registration
            self._pump_thread = Thread(
                target=self._run_message_pump,
                name="AnayaHotkeyPumpThread",
                daemon=True,
            )
            self._pump_thread.start()

        # Wait for pump thread to finish registration
        started = self._startup_event.wait(timeout=startup_timeout)
        if not started:
            logger.warning("Hotkey service startup timed out after %.1fs.", startup_timeout)
            pump_tid: int | None = None
            with self._lock:
                # Unconditional startup cancellation on timeout: state can never remain STARTING or RUNNING
                self._state = HotkeyServiceState.PUMP_FAILED
                self._stop_requested = True
                pump_tid = self._pump_thread_id

            # Outside lock, request pump termination using bounded post_thread_quit
            if pump_tid is not None:
                for _ in range(3):
                    try:
                        if self._backend.post_thread_quit(pump_tid):
                            break
                    except Exception:
                        pass
                    time.sleep(0.005)

            return False

        with self._lock:
            if self._state != HotkeyServiceState.RUNNING:
                return False

            # Registration succeeded: start dedicated non-daemon voice worker thread
            self._worker_thread = Thread(
                target=self._run_voice_worker,
                name="AnayaHotkeyVoiceWorker",
                daemon=False,
            )
            self._worker_thread.start()
            return True

    def stop(self, timeout: float = 2.0) -> None:
        """Request shutdown and perform bounded joins on message-pump and voice-worker threads.

        Enforces a single monotonic deadline across all thread joins and shutdown helpers.
        """
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise TypeError(f"timeout must be a non-negative number, got {type(timeout).__name__}")
        if not math.isfinite(timeout) or timeout < 0.0:
            raise ValueError(f"timeout must be a non-negative finite number, got {timeout}")

        deadline = time.monotonic() + float(timeout)

        with self._lock:
            if (
                self._state == HotkeyServiceState.STOPPED
                and self._pump_thread is None
                and self._worker_thread is None
                and not self._unregistration_failed
            ):
                return
            self._state = HotkeyServiceState.STOPPING
            self._stop_requested = True

        # Signal voice worker to stop
        self._worker_stop_event.set()
        self._work_trigger_event.set()

        # Signal message pump to stop via WM_QUIT with monotonic budget check
        if self._pump_thread is not None and self._pump_thread.is_alive() and self._pump_thread_id is not None:
            posted = False
            for attempt in range(3):
                try:
                    if self._backend.post_thread_quit(self._pump_thread_id):
                        posted = True
                        break
                except Exception:
                    pass
                remaining_budget = deadline - time.monotonic()
                if remaining_budget <= 0.0 or attempt == 2:
                    break
                time.sleep(min(0.005, remaining_budget))

            if not posted:
                logger.warning(
                    "Failed to post WM_QUIT to message-pump thread %d. Shutdown may be incomplete.",
                    self._pump_thread_id,
                )

        # 1. Bounded join on message-pump thread
        pump_thread = self._pump_thread
        if pump_thread is not None:
            if pump_thread.is_alive():
                remaining = max(0.0, deadline - time.monotonic())
                pump_thread.join(timeout=remaining)
            if not pump_thread.is_alive():
                with self._lock:
                    if self._pump_thread is pump_thread:
                        self._pump_thread = None
            else:
                logger.warning("Message-pump thread did not terminate within timeout budget.")

        # 2. Bounded join on voice-worker thread
        worker_thread = self._worker_thread
        if worker_thread is not None:
            if worker_thread.is_alive():
                remaining = max(0.0, deadline - time.monotonic())
                worker_thread.join(timeout=remaining)
            if not worker_thread.is_alive():
                with self._lock:
                    if self._worker_thread is worker_thread:
                        self._worker_thread = None
            else:
                logger.warning("Voice worker thread did not terminate within timeout budget.")

        # Evaluate final state
        with self._lock:
            if (
                self._pump_thread is None
                and self._worker_thread is None
                and not self._unregistration_failed
            ):
                self._state = HotkeyServiceState.STOPPED
                logger.debug("GlobalHotkeyService stopped cleanly.")
            else:
                self._state = HotkeyServiceState.TIMEOUT_STOPPED
                logger.warning(
                    "GlobalHotkeyService stop incomplete (pump_alive=%s, worker_alive=%s, unreg_failed=%s).",
                    self._pump_thread is not None,
                    self._worker_thread is not None,
                    self._unregistration_failed,
                )

    def _run_voice_worker(self) -> None:
        """Dedicated voice worker loop executing bounded voice interactions."""
        while not self._worker_stop_event.is_set():
            triggered = self._work_trigger_event.wait(timeout=0.2)
            if not triggered:
                continue

            self._work_trigger_event.clear()
            if self._worker_stop_event.is_set():
                with self._voice_lock:
                    self._voice_busy = False
                break

            # Execute voice task
            try:
                result = self._controller.handle_voice(
                    duration=self._duration,
                    speak_response=True,
                )
                if self._on_voice_result is not None:
                    try:
                        self._on_voice_result(result)
                    except Exception as cb_exc:
                        logger.debug("Error in on_voice_result callback: %s", cb_exc)
            except Exception as exc:
                logger.error("Unhandled error during hotkey voice interaction: %s", exc, exc_info=True)
            finally:
                with self._voice_lock:
                    self._voice_busy = False

    def _run_message_pump(self) -> None:
        """Dedicated message-pump thread loop owning Win32 registration and message dispatch."""
        registered = False
        try:
            self._backend.initialize_message_queue()
            self._pump_thread_id = self._backend.get_current_thread_id()

            with self._lock:
                if self._stop_requested or self._state != HotkeyServiceState.STARTING:
                    return

            registered = self._backend.register_hotkey(
                self._hotkey_id,
                self._combination.fs_modifiers,
                self._combination.vk_code,
            )

            if not registered:
                err_code = self._backend.get_last_error()
                with self._lock:
                    self._state = HotkeyServiceState.REGISTRATION_FAILED
                if err_code == 1409:
                    logger.warning(
                        "Hotkey '%s' is already registered by another application (Win32 error 1409).",
                        self._combination.canonical_string,
                    )
                else:
                    logger.error(
                        "Failed to register hotkey '%s' (Win32 error %d).",
                        self._combination.canonical_string,
                        err_code,
                    )
                self._startup_event.set()
                return

            with self._lock:
                if self._stop_requested or self._state != HotkeyServiceState.STARTING:
                    return
                self._state = HotkeyServiceState.RUNNING

            self._startup_event.set()
            logger.info("Global hotkey '%s' registered successfully.", self._combination.canonical_string)

            msg = self._backend.create_msg()
            while not self._stop_requested:
                ret = self._backend.get_message(msg)
                if ret == 0 or ret == -1:  # WM_QUIT or error
                    break

                msg_id, w_param = self._backend.extract_msg_details(msg)
                if msg_id == WM_HOTKEY and w_param == self._hotkey_id:
                    self._on_hotkey_pressed()

        except Exception as exc:
            logger.error("Fatal exception in hotkey message pump thread: %s", exc, exc_info=True)
            with self._lock:
                self._state = HotkeyServiceState.PUMP_FAILED
        finally:
            if registered:
                try:
                    unreg_ok = self._backend.unregister_hotkey(self._hotkey_id)
                    if not unreg_ok:
                        err = self._backend.get_last_error()
                        logger.error(
                            "Failed to unregister hotkey '%s' (Win32 error %d).",
                            self._combination.canonical_string,
                            err,
                        )
                        with self._lock:
                            self._unregistration_failed = True
                    else:
                        logger.debug("Hotkey '%s' unregistered on pump thread.", self._combination.canonical_string)
                except Exception as unreg_exc:
                    logger.error("Exception unregistering hotkey: %s", unreg_exc, exc_info=True)
                    with self._lock:
                        self._unregistration_failed = True

            self._startup_event.set()
            self._pump_stopped_event.set()

    def _on_hotkey_pressed(self) -> None:
        """Handle incoming WM_HOTKEY message. Drops presses if voice interaction is active or service is stopping."""
        with self._voice_lock:
            if self._voice_busy or self._worker_stop_event.is_set() or self._stop_requested:
                logger.debug("Hotkey press dropped: voice interaction is currently active or stopping.")
                return
            self._voice_busy = True

        self._work_trigger_event.set()
