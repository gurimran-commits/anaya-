"""Unit tests for FloatingHudService facade, worker lifecycle, and stop retries."""

import threading
from unittest.mock import MagicMock, patch
import pytest

from app.core.action_result import ActionResult
from app.hud.coordinator import VoiceSessionCoordinator
from app.hud.models import (
    HudErrorEvent,
    HudOperationCompletedEvent,
    HudServiceState,
    HudViewConfig,
    HudVisibilityChangedEvent,
    ResultDeliveryState,
)
from app.hud.service import FloatingHudService
from app.speech.models import PipelineResult


class FakeController:
    """Deterministic fake controller for HUD unit tests."""

    def __init__(self) -> None:
        self.text_handler = None
        self.voice_handler = None

    def handle_text(self, text: str) -> ActionResult:
        if self.text_handler:
            return self.text_handler(text)
        return ActionResult(success=True, message=f"Echo: {text}")

    def handle_voice(
        self,
        duration: float | None = None,
        speak_response: bool = False,
    ) -> PipelineResult:
        if self.voice_handler:
            return self.voice_handler(duration, speak_response)
        return PipelineResult(
            transcript="test voice input",
            action_result=ActionResult(success=True, message="Spoken response"),
            spoken_response=True,
            success=True,
        )


def test_service_initial_state() -> None:
    """Verify service initial state before start."""
    controller = FakeController()
    service = FloatingHudService(controller=controller)  # type: ignore[arg-type]

    assert service.state == HudServiceState.STOPPED
    assert not service.is_running
    assert not service.is_cleanly_stopped
    assert not service.is_worker_alive
    assert not service.is_gui_alive
    assert service.last_terminal_result is None
    assert service.delivery_state == ResultDeliveryState.NONE


def test_headless_initialization_failure() -> None:
    """Verify service handles GUI initialization failure gracefully."""
    controller = FakeController()
    service = FloatingHudService(controller=controller)  # type: ignore[arg-type]

    with patch("app.hud.service.HudGuiThread") as mock_thread_cls:
        mock_instance = MagicMock()
        mock_instance.init_event.wait.return_value = True
        mock_instance.init_error = RuntimeError("No display available (headless)")
        mock_thread_cls.return_value = mock_instance

        started = service.start()
        assert started is False
        assert service.state == HudServiceState.ERROR
        assert not service.is_running


def test_clean_start_stop_lifecycle() -> None:
    """Verify complete clean start and stop lifecycle."""
    controller = FakeController()
    config = HudViewConfig(poll_interval_ms=10)
    service = FloatingHudService(controller=controller, config=config)  # type: ignore[arg-type]

    started = service.start()
    assert started is True
    assert service.is_running
    assert service.is_gui_alive

    stopped = service.stop(timeout=3.0)
    assert stopped is True
    assert service.state == HudServiceState.STOPPED
    assert service.is_cleanly_stopped
    assert not service.is_gui_alive
    assert not service.is_worker_alive


def test_cannot_restart_stopped_service() -> None:
    """Verify single-use lifecycle: restarting stopped service raises RuntimeError."""
    controller = FakeController()
    config = HudViewConfig(poll_interval_ms=10)
    service = FloatingHudService(controller=controller, config=config)  # type: ignore[arg-type]

    assert service.start() is True
    assert service.stop(timeout=3.0) is True

    with pytest.raises(RuntimeError, match="cannot be restarted"):
        service.start()


def test_show_and_hide_visibility_events() -> None:
    """Verify show() and hide() deposit visibility events into the mailbox."""
    controller = FakeController()
    service = FloatingHudService(controller=controller)  # type: ignore[arg-type]

    service.show()
    evt1 = service.mailbox.poll_next()
    assert isinstance(evt1, HudVisibilityChangedEvent)
    assert evt1.visible is True

    service.hide()
    evt2 = service.mailbox.poll_next()
    assert isinstance(evt2, HudVisibilityChangedEvent)
    assert evt2.visible is False


def test_single_active_operation_enforcement() -> None:
    """Verify only one operation can execute simultaneously."""
    controller = FakeController()
    started_event = threading.Event()
    release_event = threading.Event()

    def slow_text_handler(text: str) -> ActionResult:
        started_event.set()
        release_event.wait(timeout=5.0)
        return ActionResult(success=True, message="Slow response")

    controller.text_handler = slow_text_handler

    config = HudViewConfig(poll_interval_ms=10)
    service = FloatingHudService(controller=controller, config=config)  # type: ignore[arg-type]
    assert service.start() is True

    try:
        # First operation succeeds in starting
        op1 = service.submit_text("query 1")
        assert op1 is not None

        # Wait until worker has started
        assert started_event.wait(timeout=2.0)

        # Second operation must be rejected
        op2 = service.submit_text("query 2")
        assert op2 is None

        # Voice operation must also be rejected
        op3 = service.trigger_voice()
        assert op3 is None

    finally:
        release_event.set()
        assert service.worker_done_event.wait(timeout=3.0)
        assert service.stop(timeout=3.0) is True


def test_stop_rejects_new_operations() -> None:
    """Verify new operations are rejected immediately during and after shutdown."""
    controller = FakeController()
    config = HudViewConfig(poll_interval_ms=10)
    service = FloatingHudService(controller=controller, config=config)  # type: ignore[arg-type]

    assert service.start() is True
    assert service.stop(timeout=3.0) is True

    assert service.submit_text("after stop") is None
    assert service.trigger_voice() is None


def test_worker_timeout_and_stop_retry() -> None:
    """Verify worker timeout leaves thread alive, and stop retry cleans up once finished."""
    controller = FakeController()
    worker_started = threading.Event()
    worker_can_finish = threading.Event()

    def hung_handler(text: str) -> ActionResult:
        worker_started.set()
        worker_can_finish.wait(timeout=5.0)
        return ActionResult(success=True, message="Finally finished")

    controller.text_handler = hung_handler

    config = HudViewConfig(poll_interval_ms=10)
    service = FloatingHudService(controller=controller, config=config)  # type: ignore[arg-type]
    assert service.start() is True

    op_id = service.submit_text("hung task")
    assert op_id is not None
    assert worker_started.wait(timeout=2.0)

    # Stop with very short timeout while worker is hung
    stopped_first = service.stop(timeout=0.1)
    assert stopped_first is False
    assert not service.is_cleanly_stopped
    assert service.is_worker_alive

    # Now release the worker
    worker_can_finish.set()
    assert service.worker_done_event.wait(timeout=2.0)

    # Retry stop with fresh monotonic budget
    stopped_retry = service.stop(timeout=3.0)
    assert stopped_retry is True
    assert service.is_cleanly_stopped
    assert not service.is_worker_alive


def test_voice_dispatch_with_coordinator() -> None:
    """Verify voice operation acquires and cleanly releases the voice lease."""
    controller = FakeController()
    coordinator = VoiceSessionCoordinator()
    config = HudViewConfig(poll_interval_ms=10)
    service = FloatingHudService(
        controller=controller,  # type: ignore[arg-type]
        coordinator=coordinator,
        config=config,
    )
    assert service.start() is True

    try:
        assert not coordinator.is_active
        op_id = service.trigger_voice()
        assert op_id is not None

        # Wait for worker completion
        assert service.worker_done_event.wait(timeout=3.0)

        # Coordinator lease should be released immediately after worker exits
        assert not coordinator.is_active

        # Terminal result should be recorded in mailbox
        result = service.last_terminal_result
        assert isinstance(result, HudOperationCompletedEvent)
        assert result.success is True
        assert result.message == "Spoken response"

    finally:
        assert service.stop(timeout=3.0) is True


def test_approval_response_rendered_as_standard_failure() -> None:
    """Option A verification: Unsuccessful approval result rendered as standard failure."""
    controller = FakeController()
    controller.text_handler = lambda text: ActionResult(
        success=False,
        message="Confirmation required. Confirmation ID: conf_test_999",
    )

    config = HudViewConfig(poll_interval_ms=10)
    service = FloatingHudService(controller=controller, config=config)  # type: ignore[arg-type]
    assert service.start() is True

    try:
        op_id = service.submit_text("delete protected resource")
        assert op_id is not None
        assert service.worker_done_event.wait(timeout=3.0)

        result = service.last_terminal_result
        assert isinstance(result, HudOperationCompletedEvent)
        assert result.success is False
        assert "Confirmation required. Confirmation ID: conf_test_999" in result.message

    finally:
        assert service.stop(timeout=3.0) is True


def test_error_sanitization_in_worker() -> None:
    """Verify unexpected controller exception is sanitized and reported as HudErrorEvent."""
    controller = FakeController()

    def crashing_handler(text: str) -> ActionResult:
        raise ValueError("Critical internal database connection lost")

    controller.text_handler = crashing_handler

    config = HudViewConfig(poll_interval_ms=10)
    service = FloatingHudService(controller=controller, config=config)  # type: ignore[arg-type]
    assert service.start() is True

    try:
        op_id = service.submit_text("crash query")
        assert op_id is not None
        assert service.worker_done_event.wait(timeout=3.0)

        err_result = service.last_terminal_result
        assert isinstance(err_result, HudErrorEvent)
        assert err_result.user_message == "Operation failed"
        assert "Critical internal database connection lost" in err_result.sanitized_details

    finally:
        assert service.stop(timeout=3.0) is True


def test_terminal_result_delivered_to_gui_during_shutdown() -> None:
    """Verify terminal result is consumed and DELIVERED when worker finishes before stop deadline."""
    controller = FakeController()
    worker_started = threading.Event()
    worker_can_finish = threading.Event()

    def coordinated_handler(text: str) -> ActionResult:
        worker_started.set()
        worker_can_finish.wait(timeout=5.0)
        return ActionResult(success=True, message="Success during shutdown")

    controller.text_handler = coordinated_handler

    config = HudViewConfig(poll_interval_ms=10)
    service = FloatingHudService(controller=controller, config=config)  # type: ignore[arg-type]
    assert service.start() is True

    op_id = service.submit_text("query during shutdown")
    assert op_id is not None
    assert worker_started.wait(timeout=2.0)

    # Trigger stop on separate thread
    stop_result: list[bool] = []

    def stop_runner() -> None:
        stop_result.append(service.stop(timeout=3.0))

    stop_thread = threading.Thread(target=stop_runner)
    stop_thread.start()

    # Verify stop is in progress
    assert service.state == HudServiceState.STOPPING

    # Release worker before stop deadline
    worker_can_finish.set()
    stop_thread.join(timeout=3.0)

    assert stop_result == [True]
    assert service.is_cleanly_stopped
    assert service.delivery_state == ResultDeliveryState.DELIVERED
    assert service.last_terminal_result is not None
    assert service.last_terminal_result.message == "Success during shutdown"


def test_terminal_result_marked_undelivered_if_gui_not_polling() -> None:
    """Verify terminal result is marked RETAINED_UNDELIVERED if GUI does not consume it."""
    controller = FakeController()
    service = FloatingHudService(controller=controller)  # type: ignore[arg-type]

    # Direct mailbox test simulating GUI thread termination before drain
    terminal_evt = HudOperationCompletedEvent(
        operation_id="op_undelivered",
        success=True,
        message="Finished but GUI missed it",
    )
    service.mailbox.put_terminal_result(terminal_evt)
    assert service.delivery_state == ResultDeliveryState.NONE

    # mark_undelivered_if_pending() runs in service.stop() after GUI join
    service.mailbox.mark_undelivered_if_pending()
    assert service.delivery_state == ResultDeliveryState.RETAINED_UNDELIVERED
    assert service.last_terminal_result == terminal_evt
    assert not service.mailbox.has_pending_terminal_result
