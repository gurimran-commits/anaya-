"""Bounded HUD event mailbox with reserved critical slots and coalescing."""

import logging
import threading

from app.hud.models import (
    HudErrorEvent,
    HudEvent,
    HudOperationCompletedEvent,
    HudShutdownEvent,
    HudStatusChangedEvent,
    HudVisibilityChangedEvent,
    ResultDeliveryState,
)

logger = logging.getLogger(__name__)


class HudEventMailbox:
    """Thread-safe bounded event mailbox with reserved slots and zero-drop critical delivery.

    Enforces constant O(1) memory by structuring communication across four dedicated slots:
        1. Coalesced Status Slot: Overwrites intermediate status updates.
        2. Coalesced Visibility Slot: Overwrites rapid show/hide toggles.
        3. Reserved Terminal-Result Slot: Exclusively holds the terminal outcome of
           the active operation. Never overwritten or dropped under N_active <= 1.
        4. Dedicated Shutdown Slot: Guaranteed delivery for service teardown,
           independent of status or terminal results.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._status_slot: HudStatusChangedEvent | None = None
        self._visibility_slot: HudVisibilityChangedEvent | None = None
        self._terminal_result_slot: HudOperationCompletedEvent | HudErrorEvent | None = None
        self._shutdown_slot: HudShutdownEvent | None = None

        # Retained result history for audit and post-shutdown inspection
        self._last_terminal_result: HudOperationCompletedEvent | HudErrorEvent | None = None
        self._delivery_state: ResultDeliveryState = ResultDeliveryState.NONE

    @property
    def last_terminal_result(self) -> HudOperationCompletedEvent | HudErrorEvent | None:
        """Inspect the most recent terminal operation result."""
        with self._lock:
            return self._last_terminal_result

    @property
    def delivery_state(self) -> ResultDeliveryState:
        """Inspect the current delivery state of the latest terminal result."""
        with self._lock:
            return self._delivery_state

    @property
    def has_pending_terminal_result(self) -> bool:
        """True if a terminal result is waiting to be consumed by the GUI thread."""
        with self._lock:
            return self._terminal_result_slot is not None

    @property
    def has_pending_shutdown(self) -> bool:
        """True if a shutdown signal is waiting to be consumed by the GUI thread."""
        with self._lock:
            return self._shutdown_slot is not None

    def put_status(self, event: HudStatusChangedEvent) -> None:
        """Deposit a status event into the coalescing status slot. Non-blocking."""
        with self._lock:
            self._status_slot = event

    def put_visibility(self, event: HudVisibilityChangedEvent) -> None:
        """Deposit a visibility event into the coalescing visibility slot. Non-blocking."""
        with self._lock:
            self._visibility_slot = event

    def put_terminal_result(self, event: HudOperationCompletedEvent | HudErrorEvent) -> None:
        """Deposit an operation terminal outcome into the reserved terminal slot.

        Non-blocking. Guaranteed delivery: never dropped under the single active
        operation invariant.
        """
        with self._lock:
            if self._terminal_result_slot is not None:
                # Should not occur under N_active <= 1; retain prior result safely
                logger.warning(
                    "Terminal slot collision detected. Superseding %s with %s",
                    self._terminal_result_slot,
                    event,
                )
            self._terminal_result_slot = event
            self._last_terminal_result = event
            self._delivery_state = ResultDeliveryState.NONE

    def put_shutdown(self) -> None:
        """Deposit a shutdown event into the dedicated control slot. Non-blocking."""
        with self._lock:
            self._shutdown_slot = HudShutdownEvent()

    def record_undelivered_result(
        self,
        event: HudOperationCompletedEvent | HudErrorEvent,
    ) -> None:
        """Record a terminal result directly as retained-undelivered.

        Used when an operation finishes after GUI shutdown has commenced or when
        the GUI thread is unresponsive.
        """
        with self._lock:
            self._last_terminal_result = event
            self._delivery_state = ResultDeliveryState.RETAINED_UNDELIVERED
            # Clear pending slot so no stale reference lingers
            self._terminal_result_slot = None

    def mark_undelivered_if_pending(self) -> None:
        """Transition any unconsumed terminal result to RETAINED_UNDELIVERED status."""
        with self._lock:
            if self._terminal_result_slot is not None:
                self._delivery_state = ResultDeliveryState.RETAINED_UNDELIVERED
                self._terminal_result_slot = None

    def poll_next(self) -> HudEvent | None:
        """Poll the next pending event in strict priority order.

        Priority Dispatch Order:
            1. Terminal Result Slot: Must be processed and rendered before GUI teardown.
               Transitions delivery state to DELIVERED.
            2. Shutdown Slot: Dispatched once terminal results are drained.
            3. Visibility Slot: Dispatched for show/hide window transitions.
            4. Status Slot: Dispatched for operational indicators.

        Returns None non-blockingly if all slots are empty.
        """
        with self._lock:
            # 1. Terminal result takes top precedence so completion is rendered before destruction
            if self._terminal_result_slot is not None:
                event = self._terminal_result_slot
                self._terminal_result_slot = None
                self._delivery_state = ResultDeliveryState.DELIVERED
                return event

            # 2. Dedicated shutdown control slot
            if self._shutdown_slot is not None:
                event = self._shutdown_slot
                self._shutdown_slot = None
                return event

            # 3. Visibility changes
            if self._visibility_slot is not None:
                event = self._visibility_slot
                self._visibility_slot = None
                return event

            # 4. Status updates
            if self._status_slot is not None:
                event = self._status_slot
                self._status_slot = None
                return event

            return None
