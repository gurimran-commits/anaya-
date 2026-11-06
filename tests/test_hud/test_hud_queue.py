"""Unit tests for HudEventMailbox reserved slots and delivery guarantees."""

import threading

from app.hud.models import (
    HudErrorEvent,
    HudOperationCompletedEvent,
    HudShutdownEvent,
    HudStatus,
    HudStatusChangedEvent,
    HudVisibilityChangedEvent,
    ResultDeliveryState,
)
from app.hud.queue import HudEventMailbox


def test_mailbox_initial_state() -> None:
    """Verify mailbox initializes with empty slots and NONE delivery state."""
    mailbox = HudEventMailbox()
    assert mailbox.poll_next() is None
    assert mailbox.last_terminal_result is None
    assert mailbox.delivery_state == ResultDeliveryState.NONE
    assert not mailbox.has_pending_terminal_result
    assert not mailbox.has_pending_shutdown


def test_mailbox_status_coalescing() -> None:
    """Verify rapid status events coalesce in Slot 1 without unbounded backlog."""
    mailbox = HudEventMailbox()
    mailbox.put_status(HudStatusChangedEvent(status=HudStatus.LISTENING, details="rec"))
    mailbox.put_status(HudStatusChangedEvent(status=HudStatus.PROCESSING, details="brain"))

    event = mailbox.poll_next()
    assert isinstance(event, HudStatusChangedEvent)
    assert event.status == HudStatus.PROCESSING
    assert event.details == "brain"

    # Slot is now empty
    assert mailbox.poll_next() is None


def test_mailbox_visibility_coalescing() -> None:
    """Verify visibility events coalesce in Slot 2."""
    mailbox = HudEventMailbox()
    mailbox.put_visibility(HudVisibilityChangedEvent(visible=False))
    mailbox.put_visibility(HudVisibilityChangedEvent(visible=True))

    event = mailbox.poll_next()
    assert isinstance(event, HudVisibilityChangedEvent)
    assert event.visible is True
    assert mailbox.poll_next() is None


def test_mailbox_terminal_result_guaranteed_delivery() -> None:
    """Verify terminal result is delivered and updates state to DELIVERED."""
    mailbox = HudEventMailbox()
    result_evt = HudOperationCompletedEvent(
        operation_id="op_test",
        success=True,
        message="Task completed",
    )

    mailbox.put_terminal_result(result_evt)
    assert mailbox.has_pending_terminal_result
    assert mailbox.delivery_state == ResultDeliveryState.NONE

    consumed = mailbox.poll_next()
    assert consumed == result_evt
    assert mailbox.delivery_state == ResultDeliveryState.DELIVERED
    assert mailbox.last_terminal_result == result_evt
    assert not mailbox.has_pending_terminal_result


def test_mailbox_ordered_drain_terminal_before_shutdown() -> None:
    """Invariant test: Terminal results are drained before shutdown signals."""
    mailbox = HudEventMailbox()
    result_evt = HudOperationCompletedEvent(
        operation_id="op_shutdown_test",
        success=True,
        message="Done before quit",
    )

    # Deposit both terminal result and shutdown signal
    mailbox.put_terminal_result(result_evt)
    mailbox.put_shutdown()

    # 1. First poll must yield terminal result
    first = mailbox.poll_next()
    assert first == result_evt
    assert mailbox.delivery_state == ResultDeliveryState.DELIVERED

    # 2. Second poll yields shutdown signal
    second = mailbox.poll_next()
    assert isinstance(second, HudShutdownEvent)

    # 3. Third poll is empty
    assert mailbox.poll_next() is None


def test_mailbox_record_undelivered_result() -> None:
    """Verify recording undelivered results directly marks RETAINED_UNDELIVERED."""
    mailbox = HudEventMailbox()
    err_evt = HudErrorEvent(
        operation_id="op_late",
        user_message="Late failure",
        sanitized_details="timeout",
    )

    mailbox.record_undelivered_result(err_evt)
    assert mailbox.last_terminal_result == err_evt
    assert mailbox.delivery_state == ResultDeliveryState.RETAINED_UNDELIVERED
    assert mailbox.poll_next() is None


def test_mailbox_mark_undelivered_if_pending() -> None:
    """Verify pending terminal results transition to RETAINED_UNDELIVERED if unread."""
    mailbox = HudEventMailbox()
    result_evt = HudOperationCompletedEvent(
        operation_id="op_abandoned",
        success=False,
        message="Unread",
    )

    mailbox.put_terminal_result(result_evt)
    assert mailbox.delivery_state == ResultDeliveryState.NONE
    assert mailbox.has_pending_terminal_result

    mailbox.mark_undelivered_if_pending()
    assert mailbox.delivery_state == ResultDeliveryState.RETAINED_UNDELIVERED
    assert not mailbox.has_pending_terminal_result
    assert mailbox.poll_next() is None


def test_mailbox_concurrent_publication() -> None:
    """Verify concurrent thread safe publication across all slots without race."""
    mailbox = HudEventMailbox()
    barrier = threading.Barrier(3)

    def status_publisher() -> None:
        barrier.wait()
        for i in range(10):
            mailbox.put_status(HudStatusChangedEvent(status=HudStatus.PROCESSING, details=str(i)))

    def visibility_publisher() -> None:
        barrier.wait()
        for i in range(10):
            mailbox.put_visibility(HudVisibilityChangedEvent(visible=(i % 2 == 0)))

    def terminal_publisher() -> None:
        barrier.wait()
        mailbox.put_terminal_result(
            HudOperationCompletedEvent(operation_id="op_conc", success=True, message="ok")
        )

    t1 = threading.Thread(target=status_publisher)
    t2 = threading.Thread(target=visibility_publisher)
    t3 = threading.Thread(target=terminal_publisher)

    t1.start()
    t2.start()
    t3.start()
    t1.join()
    t2.join()
    t3.join()

    # Draining mailbox succeeds safely
    events = []
    while True:
        evt = mailbox.poll_next()
        if evt is None:
            break
        events.append(evt)

    assert len(events) >= 1
    assert any(isinstance(e, HudOperationCompletedEvent) for e in events)
