"""Application runtime coordinator for the Anaya assistant.

Integrates command infrastructure, TextAssistant, TaskManager, TaskScheduler,
and NotificationHandler into a unified, resource-conscious lifecycle.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Any, Callable

from app.commands.executor import CommandExecutor
from app.commands.registry import CommandRegistry
from app.commands.task_commands import register_task_commands
from app.core.action_result import ActionResult
from app.core.config import DEFAULT_CONFIG, AppConfig
from app.notifications.handlers import (
    CallbackNotificationHandler,
    LoggingNotificationHandler,
)
from app.notifications.models import ReminderNotification
from app.notifications.protocols import NotificationHandler
from app.security.approval_service import ApprovalService
from app.tasks.models import Task
from app.tasks.scheduler import TaskScheduler
from app.tasks.task_manager import TaskManager

if TYPE_CHECKING:
    from app.ai.manager import ModelManager
    from app.brain.text_assistant import TextAssistant
    from app.memory.memory_manager import MemoryManager
    from app.skills.skill_manager import SkillManager

logger = logging.getLogger(__name__)


class AnayaRuntime:
    """Runtime coordinator orchestrating services, scheduler, and notifications.

    Guarantees:
        - Exactly one background scheduler thread when active.
        - Idempotent start() and stop() lifecycle.
        - Automatic missed-reminder recovery during startup.
        - Deterministic at-most-once delivery across crashes via atomic SQLite claims.
        - Zero thread creation on import or object construction.
    """

    def __init__(
        self,
        config: AppConfig | None = None,
        registry: CommandRegistry | None = None,
        executor: CommandExecutor | None = None,
        approval_service: ApprovalService | None = None,
        task_manager: TaskManager | None = None,
        scheduler: TaskScheduler | None = None,
        text_assistant: "TextAssistant | None" = None,
        model_manager: "ModelManager | None" = None,
        memory_manager: "MemoryManager | None" = None,
        skill_manager: "SkillManager | None" = None,
        notification_handler: NotificationHandler | None = None,
        on_reminder: Callable[[ReminderNotification], None] | None = None,
        clock: Callable[[], datetime] | None = None,
        db_path: Path | str | None = None,
    ) -> None:
        self._config = config or DEFAULT_CONFIG
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = RLock()
        self._running = False
        self._task_manager: TaskManager | None = None

        try:
            # 1. Core security and command execution infrastructure
            self._approval_service = approval_service or ApprovalService()
            self._registry = registry or CommandRegistry()
            self._executor = executor or CommandExecutor(
                self._registry,
                approval_service=self._approval_service,
            )

            # 2. Task management and command registration
            self._task_manager = task_manager or TaskManager(
                db_path=db_path,
                clock=self._clock,
            )
            register_task_commands(self._registry, self._task_manager)

            # 3. Notification handler composition
            if on_reminder is not None:
                self._notification_handler: NotificationHandler = (
                    CallbackNotificationHandler(on_reminder)
                )
            elif notification_handler is not None:
                self._notification_handler = notification_handler
            else:
                self._notification_handler = LoggingNotificationHandler()

            # 4. Background reminder scheduler
            self._scheduler = scheduler or TaskScheduler(
                task_manager=self._task_manager,
                on_reminder=self._handle_scheduler_reminder,
                poll_interval=self._config.task_scheduler_poll_interval,
                clock=self._clock,
            )

            # 5. Optional subsystems
            self._model_manager = model_manager
            self._memory_manager = memory_manager
            self._skill_manager = skill_manager

            # 6. Natural language & Text assistant
            if text_assistant is not None:
                self._text_assistant = text_assistant
            else:
                from app.brain.text_assistant import TextAssistant

                self._text_assistant = TextAssistant(
                    registry=self._registry,
                    executor=self._executor,
                    task_manager=self._task_manager,
                    model_manager=self._model_manager,
                    memory_manager=self._memory_manager,
                    skill_manager=self._skill_manager,
                )
        except Exception:
            if self._task_manager is not None:
                try:
                    self._task_manager.close()
                except Exception:
                    pass
            raise

    @property
    def config(self) -> AppConfig:
        """Return application configuration."""
        return self._config

    @property
    def is_running(self) -> bool:
        """Return True if runtime is actively running."""
        with self._lock:
            return self._running

    @property
    def task_manager(self) -> TaskManager:
        """Expose domain TaskManager."""
        return self._task_manager

    @property
    def scheduler(self) -> TaskScheduler:
        """Expose background TaskScheduler."""
        return self._scheduler

    @property
    def text_assistant(self) -> "TextAssistant":
        """Expose integrated TextAssistant."""
        return self._text_assistant

    @property
    def registry(self) -> CommandRegistry:
        """Expose CommandRegistry."""
        return self._registry

    @property
    def executor(self) -> CommandExecutor:
        """Expose CommandExecutor."""
        return self._executor

    @property
    def approval_service(self) -> ApprovalService:
        """Expose ApprovalService."""
        return self._approval_service

    @property
    def notification_handler(self) -> NotificationHandler:
        """Expose NotificationHandler."""
        return self._notification_handler

    @property
    def model_manager(self) -> "ModelManager | None":
        """Expose optional local AI ModelManager."""
        return self._model_manager

    def start(self) -> None:
        """Start the application runtime and background scheduler.

        Idempotent: safe to call multiple times without spawning duplicate threads.
        """
        with self._lock:
            if self._running:
                return

            if self._scheduler is not None and self._scheduler.is_running:
                raise RuntimeError(
                    "Cannot start AnayaRuntime: previous scheduler worker thread is still active."
                )

            try:
                # Reopen task store if previously closed by stop()
                if self._task_manager is not None and getattr(self._task_manager.store, "_closed", False):
                    db_p = self._task_manager.store.db_path
                    self._task_manager = TaskManager(db_path=db_p, clock=self._clock)
                    if self._scheduler is not None:
                        self._scheduler._task_manager = self._task_manager
                    if self._text_assistant is not None:
                        self._text_assistant._task_manager = self._task_manager
                    register_task_commands(self._registry, self._task_manager)

                # 1. Recover missed reminders that occurred during application downtime
                if (
                    self._config.task_recover_missed_reminders
                    and self._task_manager is not None
                ):
                    self.recover_missed_reminders()

                # 2. Start the background scheduler worker thread if enabled
                if self._config.task_scheduling_enabled and self._scheduler is not None:
                    self._scheduler.start()

                self._running = True
                logger.info("AnayaRuntime started successfully.")
            except Exception as exc:
                logger.error("Failed to start AnayaRuntime: %s", exc)
                self.stop()
                raise

    def stop(self, timeout: float = 5.0) -> None:
        """Stop runtime services and cleanly join background threads.

        Idempotent: safe to call multiple times.
        """
        with self._lock:
            if not self._running and (
                self._scheduler is None or not self._scheduler.is_running
            ):
                if self._task_manager is not None and not getattr(self._task_manager.store, "_closed", False):
                    try:
                        self._task_manager.close()
                    except Exception as exc:
                        logger.error("Error closing TaskManager store: %s", exc)
                return

            try:
                if self._scheduler is not None and self._scheduler.is_running:
                    self._scheduler.stop(timeout=timeout)
            except Exception as exc:
                logger.error("Error stopping scheduler: %s", exc)

            # Only close TaskManager store if scheduler worker has cleanly stopped.
            # If the worker is still alive after timeout, closing the database connection
            # while the worker might be querying SQLite would cause crashes or corruption.
            if self._scheduler is None or not self._scheduler.is_running:
                try:
                    if self._task_manager is not None:
                        self._task_manager.close()
                except Exception as exc:
                    logger.error("Error closing TaskManager store: %s", exc)

                self._running = False
                logger.info("AnayaRuntime stopped cleanly.")
            else:
                self._running = False
                logger.warning(
                    "Scheduler worker still running after %.1fs timeout; TaskManager store left open to avoid crashing active worker.",
                    timeout,
                )

    def recover_missed_reminders(self) -> int:
        """Identify and deliver reminders that became due while Anaya was closed.

        Returns:
            Number of missed reminders recovered and dispatched.
        """
        if self._task_manager is None:
            return 0

        ref_time = self._clock()
        missed = self._task_manager.get_missed_reminders(ref_time)
        recovered_count = 0

        for task in missed:
            # Atomically claim before dispatching to eliminate duplicate delivery
            claimed = self._task_manager.claim_reminder(task.id)
            if not claimed:
                continue

            notification = ReminderNotification.from_task(
                task,
                is_missed=True,
            )
            self._dispatch_notification(notification)
            recovered_count += 1

        if recovered_count > 0:
            logger.info("Recovered and dispatched %d missed reminders.", recovered_count)
        return recovered_count

    def run_scheduler_pending(self, now: datetime | None = None) -> int:
        """Synchronously trigger pending reminder checks.

        Facilitates deterministic manual ticks in tests and CLI invocations.
        """
        if self._scheduler is not None:
            return self._scheduler.run_pending(now=now)
        return 0

    def process(self, text: str) -> ActionResult:
        """Process user text input through the configured TextAssistant."""
        return self._text_assistant.process(text)

    def _handle_scheduler_reminder(self, task: Task) -> None:
        """Callback invoked by TaskScheduler when a normal reminder is due."""
        notification = ReminderNotification.from_task(task, is_missed=False)
        self._dispatch_notification(notification)

    def _dispatch_notification(self, notification: ReminderNotification) -> None:
        """Deliver notification to the configured handler safely."""
        if self._notification_handler is None:
            return

        try:
            self._notification_handler.notify(notification)
        except Exception as exc:
            logger.error(
                "Error delivering reminder notification for task '%s': %s",
                notification.task_id,
                exc,
            )

    def __enter__(self) -> AnayaRuntime:
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.stop()
