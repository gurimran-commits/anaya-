"""Floating Assistant HUD service facade, background worker, and lifecycle coordinator."""

import logging
import threading
import time
from typing import TYPE_CHECKING

from app.hud.coordinator import VoiceLease, VoiceSessionCoordinator
from app.hud.models import (
    HudErrorEvent,
    HudOperationCompletedEvent,
    HudServiceState,
    HudStatus,
    HudStatusChangedEvent,
    HudViewConfig,
    HudVisibilityChangedEvent,
    ResultDeliveryState,
    VoiceSessionSource,
    generate_operation_id,
)
from app.hud.queue import HudEventMailbox
from app.hud.view import HudGuiThread

if TYPE_CHECKING:
    from app.controller import AssistantController

logger = logging.getLogger(__name__)


class FloatingHudService:
    """Presentation-layer facade orchestrating the Floating Assistant HUD.

    Coordinates between the AssistantController, VoiceSessionCoordinator,
    HudEventMailbox, and dedicated HudGuiThread while preserving strict single-thread
    Tkinter confinement and bounded single-active-operation execution.
    """

    def __init__(
        self,
        controller: "AssistantController",
        coordinator: VoiceSessionCoordinator | None = None,
        config: HudViewConfig | None = None,
    ) -> None:
        self._controller = controller
        self._coordinator = coordinator or VoiceSessionCoordinator()
        self._config = config or HudViewConfig()
        self._mailbox = HudEventMailbox()

        # Thread synchronization
        self._lock = threading.RLock()
        self._op_lock = threading.Lock()

        # Lifecycle state
        self._state = HudServiceState.STOPPED
        self._stop_requested = False
        self._cleanly_stopped = False
        self._stopped_previously = False

        # Active operation tracking
        self._active_op_id: str | None = None
        self._worker_thread: threading.Thread | None = None
        self._worker_done_event = threading.Event()
        self._worker_done_event.set()  # Initially idle

        # GUI Thread
        self._gui_thread: HudGuiThread | None = None

    @property
    def state(self) -> HudServiceState:
        """Current operational lifecycle state."""
        with self._lock:
            return self._state

    @property
    def is_running(self) -> bool:
        """True strictly while the HUD service is active and running."""
        with self._lock:
            return self._state == HudServiceState.RUNNING

    @property
    def is_cleanly_stopped(self) -> bool:
        """True if all managed threads (GUI and worker) terminated cleanly."""
        with self._lock:
            return self._cleanly_stopped

    @property
    def is_worker_alive(self) -> bool:
        """True while an asynchronous controller worker thread is actively executing."""
        with self._op_lock:
            return self._worker_thread is not None and self._worker_thread.is_alive()

    @property
    def is_gui_alive(self) -> bool:
        """True while the dedicated Tkinter GUI thread is actively running."""
        with self._lock:
            return self._gui_thread is not None and self._gui_thread.is_alive()

    @property
    def worker_done_event(self) -> threading.Event:
        """Event signaled when the active worker completes (allows non-GUI observation)."""
        return self._worker_done_event

    @property
    def coordinator(self) -> VoiceSessionCoordinator:
        """Expose the shared VoiceSessionCoordinator."""
        return self._coordinator

    @property
    def mailbox(self) -> HudEventMailbox:
        """Expose the bounded HudEventMailbox."""
        return self._mailbox

    @property
    def last_terminal_result(self) -> HudOperationCompletedEvent | HudErrorEvent | None:
        """Inspect the most recent terminal operation result."""
        return self._mailbox.last_terminal_result

    @property
    def delivery_state(self) -> ResultDeliveryState:
        """Inspect the delivery state of the latest terminal result."""
        return self._mailbox.delivery_state

    # ========================================================================
    # Service Lifecycle
    # ========================================================================

    def start(self) -> bool:
        """Start the HUD GUI thread and message pump.

        Returns True if the service successfully initialized and entered RUNNING.
        Returns False if GUI initialization failed (e.g. headless environment).
        Raises RuntimeError if restarting an already-stopped instance.
        """
        with self._lock:
            if self._stopped_previously:
                raise RuntimeError(
                    "FloatingHudService has been stopped and cannot be restarted. "
                    "Create a new instance instead."
                )

            if self.is_worker_alive or self.is_gui_alive:
                raise RuntimeError(
                    "Cannot start FloatingHudService: previous worker or GUI thread is still active."
                )

            if self._state == HudServiceState.RUNNING:
                return True

            self._state = HudServiceState.STARTING
            self._stop_requested = False
            self._cleanly_stopped = False

            # Spawn dedicated GUI thread
            self._gui_thread = HudGuiThread(
                mailbox=self._mailbox,
                config=self._config,
                on_text_submit=self._on_gui_text_submit,
                on_voice_click=self._on_gui_voice_click,
            )
            self._gui_thread.start()

            # Await GUI initialization
            initialized = self._gui_thread.init_event.wait(timeout=3.0)
            if not initialized:
                logger.error("Timed out waiting for HUD GUI thread initialization")
                self._state = HudServiceState.ERROR
                return False

            if self._gui_thread.init_error is not None:
                logger.warning(
                    "HUD GUI thread failed to initialize (e.g. headless environment): %s",
                    self._gui_thread.init_error,
                )
                self._state = HudServiceState.ERROR
                return False

            self._state = HudServiceState.RUNNING
            logger.info("FloatingHudService started successfully")
            return True

    def stop(self, timeout: float = 5.0) -> bool:
        """Gracefully stop the HUD service and terminate its owned threads.

        Uses a monotonic timeout budget. Retains thread references if any worker
        or GUI thread fails to terminate within the deadline.

        Returns True if all owned threads terminated cleanly; False if incomplete.
        """
        deadline = time.monotonic() + max(0.1, timeout)

        with self._lock:
            self._stop_requested = True
            self._stopped_previously = True
            self._state = HudServiceState.STOPPING

        # 1. Inform GUI visually that shutdown is in progress
        self._mailbox.put_status(
            HudStatusChangedEvent(status=HudStatus.STOPPING, details="Shutting down...")
        )

        # 2. Join active worker thread if running
        with self._op_lock:
            worker = self._worker_thread

        if worker is not None and worker.is_alive():
            remaining_worker = max(0.0, deadline - time.monotonic())
            worker.join(timeout=remaining_worker)
            if worker.is_alive():
                logger.warning("HUD worker thread timed out during shutdown")
                with self._lock:
                    self._cleanly_stopped = False
                return False

        # 3. Worker is dead; signal GUI thread to exit cleanly
        self._mailbox.put_shutdown()

        # 4. Join GUI thread
        gui = self._gui_thread
        if gui is not None and gui.is_alive():
            remaining_gui = max(0.0, deadline - time.monotonic())
            gui.join(timeout=remaining_gui)
            if gui.is_alive():
                logger.warning("HUD GUI thread timed out during shutdown")
                with self._lock:
                    self._cleanly_stopped = False
                return False

        # 5. Check if any pending unread terminal result should be marked undelivered
        self._mailbox.mark_undelivered_if_pending()

        # 6. Verify clean shutdown
        worker_dead = worker is None or not worker.is_alive()
        gui_dead = gui is None or not gui.is_alive()

        with self._lock:
            if worker_dead and gui_dead:
                self._state = HudServiceState.STOPPED
                self._cleanly_stopped = True
                logger.info("FloatingHudService stopped cleanly")
                return True
            else:
                self._cleanly_stopped = False
                return False

    def show(self) -> None:
        """Request the HUD window to be shown on screen."""
        self._mailbox.put_visibility(HudVisibilityChangedEvent(visible=True))

    def hide(self) -> None:
        """Request the HUD window to be hidden."""
        self._mailbox.put_visibility(HudVisibilityChangedEvent(visible=False))

    # ========================================================================
    # Operation Dispatching
    # ========================================================================

    def submit_text(self, text: str) -> str | None:
        """Submit text for natural language processing through AssistantController.

        Returns the operation_id if accepted; returns None if busy or stopping.
        """
        with self._lock:
            if self._stop_requested or self._state != HudServiceState.RUNNING:
                logger.debug("submit_text rejected: service is not running or stopping")
                return None

        with self._op_lock:
            if self._active_op_id is not None or (
                self._worker_thread is not None and self._worker_thread.is_alive()
            ):
                logger.debug("submit_text rejected: another operation is active")
                return None

            operation_id = generate_operation_id()
            self._active_op_id = operation_id
            self._worker_done_event.clear()

            self._worker_thread = threading.Thread(
                target=self._execute_text_worker,
                args=(operation_id, text),
                name="AnayaHudTextWorker",
                daemon=False,
            )
            self._worker_thread.start()
            return operation_id

    def trigger_voice(self, duration: float | None = None) -> str | None:
        """Initiate push-to-talk voice capture through AssistantController.

        Atomically acquires a VoiceLease from the VoiceSessionCoordinator.
        Returns operation_id if accepted; returns None if busy, lease unavailable, or stopping.
        """
        with self._lock:
            if self._stop_requested or self._state != HudServiceState.RUNNING:
                logger.debug("trigger_voice rejected: service is not running or stopping")
                return None

        # Atomically acquire voice lease before claiming active operation
        lease = self._coordinator.acquire(VoiceSessionSource.HUD_BUTTON)
        if lease is None:
            logger.debug("trigger_voice rejected: microphone session busy")
            return None

        with self._op_lock:
            if self._active_op_id is not None or (
                self._worker_thread is not None and self._worker_thread.is_alive()
            ):
                logger.debug("trigger_voice rejected: another operation is active")
                self._coordinator.release(lease)
                return None

            operation_id = generate_operation_id()
            self._active_op_id = operation_id
            self._worker_done_event.clear()

            self._worker_thread = threading.Thread(
                target=self._execute_voice_worker,
                args=(operation_id, lease, duration),
                name="AnayaHudVoiceWorker",
                daemon=False,
            )
            self._worker_thread.start()
            return operation_id

    # ========================================================================
    # Background Workers
    # ========================================================================

    def _execute_text_worker(self, operation_id: str, text: str) -> None:
        """Worker thread executing text processing via AssistantController."""
        try:
            self._mailbox.put_status(
                HudStatusChangedEvent(status=HudStatus.PROCESSING, details="Processing...")
            )
            action_result = self._controller.handle_text(text)

            event = HudOperationCompletedEvent(
                operation_id=operation_id,
                success=action_result.success,
                message=action_result.message,
            )
            self._mailbox.put_terminal_result(event)

        except Exception as exc:
            logger.error("Unhandled error during HUD text processing: %s", exc, exc_info=True)
            err_event = HudErrorEvent(
                operation_id=operation_id,
                user_message="Operation failed",
                sanitized_details=str(exc),
            )
            self._mailbox.put_terminal_result(err_event)

        finally:
            with self._op_lock:
                if self._active_op_id == operation_id:
                    self._active_op_id = None
            self._worker_done_event.set()

    def _execute_voice_worker(
        self,
        operation_id: str,
        lease: VoiceLease,
        duration: float | None,
    ) -> None:
        """Worker thread executing push-to-talk recording and pipeline via AssistantController."""
        try:
            # Voice pipeline execution is synchronous: audio record -> transcribe -> assistant -> TTS
            self._mailbox.put_status(
                HudStatusChangedEvent(status=HudStatus.PROCESSING, details="Processing voice...")
            )
            pipeline_result = self._controller.handle_voice(
                duration=duration,
                speak_response=True,
            )

            if pipeline_result.action_result is not None:
                msg = pipeline_result.action_result.message
                success = pipeline_result.action_result.success
            elif pipeline_result.error is not None:
                msg = pipeline_result.error
                success = False
            else:
                msg = "Voice interaction completed."
                success = pipeline_result.success

            event = HudOperationCompletedEvent(
                operation_id=operation_id,
                success=success,
                message=msg,
            )
            self._mailbox.put_terminal_result(event)

        except Exception as exc:
            logger.error("Unhandled error during HUD voice processing: %s", exc, exc_info=True)
            err_event = HudErrorEvent(
                operation_id=operation_id,
                user_message="Voice operation failed",
                sanitized_details=str(exc),
            )
            self._mailbox.put_terminal_result(err_event)

        finally:
            # Release microphone lease immediately when audio operation ends
            self._coordinator.release(lease)
            with self._op_lock:
                if self._active_op_id == operation_id:
                    self._active_op_id = None
            self._worker_done_event.set()

    # ========================================================================
    # GUI Callback Handlers (Invoked on GUI thread, dispatch to workers)
    # ========================================================================

    def _on_gui_text_submit(self, text: str) -> None:
        """Callback invoked by the GUI thread when the user submits text."""
        op_id = self.submit_text(text)
        if op_id is None:
            self._mailbox.put_terminal_result(
                HudErrorEvent(
                    operation_id=None,
                    user_message="Assistant is busy or unavailable.",
                )
            )

    def _on_gui_voice_click(self) -> None:
        """Callback invoked by the GUI thread when the user clicks the voice button."""
        op_id = self.trigger_voice()
        if op_id is None:
            self._mailbox.put_terminal_result(
                HudErrorEvent(
                    operation_id=None,
                    user_message="Microphone is currently in use or unavailable.",
                )
            )
