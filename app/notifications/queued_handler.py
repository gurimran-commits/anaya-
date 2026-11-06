"""Asynchronous queued notification adapter providing scheduler thread isolation."""

from __future__ import annotations

import logging
import queue
from threading import Event, RLock, Thread
from typing import Any

from app.notifications.handlers import LoggingNotificationHandler
from app.notifications.models import ReminderNotification
from app.notifications.protocols import NotificationHandler

logger = logging.getLogger(__name__)

# Sentinel object used to signal shutdown to the worker thread
_SHUTDOWN_SENTINEL = object()


class QueuedNotificationHandler:
    """Bounded, thread-isolated notification adapter wrapping a target NotificationHandler.

    Guarantees:
        - Asynchronous dispatch: notify() uses put_nowait() to submit items into a bounded FIFO queue.
        - Scheduler isolation: Downstream notification delivery (e.g. Windows Toasts, network calls)
          is performed exclusively on a dedicated background worker thread, never on the scheduler loop.
        - Best-effort delivery: On queue overflow (queue.Full), notifications fall back to best-effort
          logging via LoggingNotificationHandler. In-memory queue items may be lost on abrupt process
          termination or unhandled crashes; this adapter does not guarantee zero loss.
        - Delivery fault tolerance: Downstream delivery failures are caught, logged, and forwarded
          to the fallback logging handler.
        - Fail-safe logging fallback: Any failure within the fallback logging path is caught and suppressed,
          ensuring exceptions never bubble up to interrupt the caller or scheduler loop.
        - Bounded shutdown: stop() signals the worker, drains pending items within the timeout, and reports
          a warning if the worker thread fails to exit within the requested timeout.
    """

    def __init__(
        self,
        target_handler: NotificationHandler,
        fallback_handler: NotificationHandler | None = None,
        max_queue_size: int = 100,
        worker_name: str = "AnayaNotificationWorker",
    ) -> None:
        if not isinstance(max_queue_size, int) or max_queue_size <= 0:
            raise ValueError(f"max_queue_size must be a positive integer, got {max_queue_size!r}")

        self._target_handler = target_handler
        self._fallback_handler = fallback_handler or LoggingNotificationHandler()
        self._max_queue_size = max_queue_size
        self._worker_name = worker_name

        self._queue: queue.Queue[Any] = queue.Queue(maxsize=max_queue_size)
        self._stop_event = Event()
        self._worker_thread: Thread | None = None
        self._lock = RLock()
        self._stopped = False

    @property
    def target_handler(self) -> NotificationHandler:
        """Expose the downstream target notification handler."""
        return self._target_handler

    @property
    def fallback_handler(self) -> NotificationHandler:
        """Expose the fallback logging notification handler."""
        return self._fallback_handler

    @property
    def is_running(self) -> bool:
        """Return True if background worker thread is active."""
        with self._lock:
            return self._worker_thread is not None and self._worker_thread.is_alive()

    @property
    def pending_count(self) -> int:
        """Return approximate number of pending items in the queue."""
        return self._queue.qsize()

    def start(self) -> None:
        """Explicitly start background notification worker thread.

        Safe to call multiple times; no-op if already running.
        """
        with self._lock:
            if self._stopped:
                raise RuntimeError("QueuedNotificationHandler has been stopped and cannot be restarted.")
            if self.is_running:
                return
            self._start_worker_locked()

    def _start_worker_locked(self) -> None:
        """Spawn the worker thread. Caller must hold self._lock."""
        self._stop_event.clear()
        self._worker_thread = Thread(
            target=self._run_worker,
            name=self._worker_name,
            daemon=True,
        )
        self._worker_thread.start()
        logger.debug("QueuedNotificationHandler worker thread %s started.", self._worker_name)

    def notify(self, notification: ReminderNotification) -> None:
        """Submit a reminder notification for asynchronous delivery.

        Uses non-blocking put_nowait(). If the queue is full or handler is stopped,
        routes to fallback logging. Exceptions in logging are caught and suppressed.
        """
        if not isinstance(notification, ReminderNotification):
            self._safe_fallback_log(
                notification,
                reason="Invalid notification payload: expected ReminderNotification instance.",
            )
            return

        with self._lock:
            if self._stopped:
                self._safe_fallback_log(
                    notification,
                    reason="QueuedNotificationHandler is stopped; routed to fallback logging.",
                )
                return

            if not self.is_running:
                self._start_worker_locked()

        try:
            self._queue.put_nowait(notification)
        except queue.Full:
            self._safe_fallback_log(
                notification,
                reason=(
                    f"Notification queue full (capacity={self._max_queue_size}); "
                    f"routed reminder '{notification.task_id}' to fallback logging."
                ),
                is_warning=True,
            )

    def stop(self, timeout: float = 2.0) -> None:
        """Signal worker thread to stop and drain remaining items within timeout."""
        with self._lock:
            self._stopped = True
            if not self.is_running:
                return

        self._stop_event.set()

        # Attempt to insert sentinel to unblock worker if waiting on empty queue
        try:
            self._queue.put_nowait(_SHUTDOWN_SENTINEL)
        except queue.Full:
            pass

        if self._worker_thread is not None and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=timeout)
            if self._worker_thread.is_alive():
                logger.warning(
                    "QueuedNotificationHandler worker thread %s did not terminate within timeout %.1fs.",
                    self._worker_thread.name,
                    timeout,
                )
            else:
                self._worker_thread = None

        # Drain any remaining items that were not delivered before worker shutdown
        self._drain_remaining_to_fallback()

    def _run_worker(self) -> None:
        """Main worker loop executed by the background daemon thread."""
        while not self._stop_event.is_set():
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue

            if item is _SHUTDOWN_SENTINEL:
                self._queue.task_done()
                break

            self._deliver_item(item)
            self._queue.task_done()

    def _deliver_item(self, notification: ReminderNotification) -> None:
        """Deliver a single notification to the target handler with fallback on error."""
        try:
            self._target_handler.notify(notification)
        except Exception as exc:
            logger.error(
                "Target notification handler %r failed to deliver reminder '%s': %s",
                self._target_handler,
                notification.task_id,
                exc,
                exc_info=True,
            )
            self._safe_fallback_log(
                notification,
                reason=(
                    f"Downstream delivery failure ({type(exc).__name__}: {exc}); "
                    f"routed reminder '{notification.task_id}' to fallback logging."
                ),
            )

    def _drain_remaining_to_fallback(self) -> None:
        """Drain any unconsumed items from the queue to the fallback logger."""
        drained_count = 0
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break

            if item is not _SHUTDOWN_SENTINEL and isinstance(item, ReminderNotification):
                drained_count += 1
                self._safe_fallback_log(
                    item,
                    reason=f"QueuedNotificationHandler shutdown; flushed pending reminder '{item.task_id}' to logging.",
                )
            self._queue.task_done()

        if drained_count > 0:
            logger.info("Drained %d pending notifications to fallback logging on shutdown.", drained_count)

    def _safe_fallback_log(
        self,
        notification: Any,
        reason: str,
        is_warning: bool = False,
    ) -> None:
        """Safely invoke fallback logging, catching and suppressing any logging exceptions.

        Guarantees that a failure in logging or diagnostic output will never raise an
        unhandled exception back to the caller or scheduler.
        """
        try:
            if is_warning:
                logger.warning("%s", reason)
            else:
                logger.info("%s", reason)

            if isinstance(notification, ReminderNotification) and self._fallback_handler is not None:
                self._fallback_handler.notify(notification)
        except Exception as exc:
            # Defensively catch and suppress any logging failure
            try:
                logger.critical("Critical failure in notification fallback handler: %s", exc)
            except Exception:
                pass
