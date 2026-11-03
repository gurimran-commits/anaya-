"""In-process background reminder scheduler using standard-library threading."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from threading import Event, RLock, Thread
from typing import Callable

from app.tasks.models import Task
from app.tasks.task_manager import TaskManager

logger = logging.getLogger(__name__)


class TaskScheduler:
    """Lightweight in-process worker polling for due task reminders."""

    def __init__(
        self,
        task_manager: TaskManager,
        on_reminder: Callable[[Task], None],
        poll_interval: float = 15.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._task_manager = task_manager
        self._on_reminder = on_reminder
        self._poll_interval = max(0.1, float(poll_interval))
        self._clock = clock or (lambda: datetime.now(timezone.utc))

        self._stop_event = Event()
        self._worker_thread: Thread | None = None
        self._lock = RLock()

    @property
    def is_running(self) -> bool:
        """Return True if background worker thread is active."""
        with self._lock:
            return self._worker_thread is not None and self._worker_thread.is_alive()

    def start(self) -> None:
        """Explicitly start background reminder thread.

        Safe to call multiple times; no-op if already running.
        """
        with self._lock:
            if self.is_running:
                return

            self._stop_event.clear()
            self._worker_thread = Thread(
                target=self._run_loop,
                name="AnayaTaskSchedulerWorker",
                daemon=True,
            )
            self._worker_thread.start()
            logger.info("TaskScheduler background worker started.")

    def stop(self, timeout: float = 5.0) -> None:
        """Signal worker thread to stop and wait for termination."""
        with self._lock:
            if not self.is_running:
                return

            self._stop_event.set()
            if self._worker_thread is not None:
                self._worker_thread.join(timeout=timeout)
                self._worker_thread = None
            logger.info("TaskScheduler background worker stopped.")

    def run_pending(self, now: datetime | None = None) -> int:
        """Synchronously check and dispatch due reminders.

        Useful for manual invocation, tests, and deterministic polling ticks.

        Returns:
            Number of reminders claimed and dispatched.
        """
        current_time = now or self._clock()
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)

        due_tasks = self._task_manager.get_due_reminders(current_time)
        dispatched_count = 0

        for task in due_tasks:
            # Atomically claim before dispatching to eliminate duplicate delivery
            claimed = self._task_manager.claim_reminder(task.id)
            if not claimed:
                continue

            try:
                self._on_reminder(task)
                dispatched_count += 1
            except Exception as exc:
                # Callback failures must never crash the scheduler loop
                logger.error(
                    "Error executing reminder callback for task '%s': %s",
                    task.id,
                    exc,
                )

        return dispatched_count

    def _run_loop(self) -> None:
        """Main loop executed by the daemon thread."""
        while not self._stop_event.is_set():
            try:
                self.run_pending()
            except Exception as exc:
                logger.error("Unexpected error in TaskScheduler loop: %s", exc)

            # Wait on event for poll_interval, enabling immediate shutdown when set
            self._stop_event.wait(self._poll_interval)
