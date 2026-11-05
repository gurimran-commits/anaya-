"""Task Manager domain service handling task validation, lifecycle, and business logic."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from app.tasks.date_parser import DateParser
from app.tasks.models import (
    Task,
    TaskConflictError,
    TaskNotFoundError,
    TaskPriority,
    TaskStatus,
    TaskValidationError,
)
from app.tasks.task_store import TaskStore


class TaskManager:
    """Core domain manager for user tasks and reminders."""

    def __init__(
        self,
        store: TaskStore | None = None,
        db_path: Path | str | None = None,
        clock: Callable[[], datetime] | None = None,
        date_parser: DateParser | None = None,
    ) -> None:
        self._store = store or TaskStore(db_path=db_path)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._date_parser = date_parser or DateParser()

    @property
    def store(self) -> TaskStore:
        """Expose underlying store for testing and inspection."""
        return self._store

    def _now(self) -> datetime:
        """Return the current time, ensuring timezone awareness in UTC."""
        current = self._clock()
        if current.tzinfo is None:
            return current.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc)

    def _parse_datetime(
        self,
        val: datetime | str | None,
        is_deadline: bool,
    ) -> datetime | None:
        """Convert a datetime or string representation into a timezone-aware UTC datetime."""
        if val is None:
            return None
        if isinstance(val, datetime):
            if val.tzinfo is None:
                raise TaskValidationError(
                    "Naive datetime objects are not accepted. Supply a timezone-aware datetime."
                )
            return val.astimezone(timezone.utc)
        if isinstance(val, str):
            parsed = self._date_parser.parse(
                val,
                reference_time=self._now(),
                is_deadline=is_deadline,
            )
            if parsed is None:
                raise TaskValidationError(
                    f"Could not parse '{val}' as a valid date or time."
                )
            return parsed
        raise TaskValidationError(
            f"Expected datetime, string, or None, got {type(val).__name__}."
        )

    def _generate_task_id(self) -> str:
        """Generate a short, collision-resistant 8-character hex task ID."""
        for _ in range(5):
            candidate = uuid4().hex[:8]
            if self._store.get_task(candidate) is None:
                return candidate
        raise TaskConflictError("Failed to generate a unique task ID after multiple attempts.")

    def create_task(
        self,
        title: str,
        description: str | None = None,
        due_date: datetime | str | None = None,
        reminder_at: datetime | str | None = None,
        priority: str = TaskPriority.MEDIUM.value,
        metadata: dict[str, Any] | None = None,
    ) -> Task:
        """Create and persist a new task.

        Separation of concerns:
            due_date represents the task's deadline.
            reminder_at represents when Anaya should alert the user.
        """
        if not isinstance(title, str) or not title.strip():
            raise TaskValidationError("Task title cannot be empty.")

        parsed_due = self._parse_datetime(due_date, is_deadline=True)
        parsed_reminder = self._parse_datetime(reminder_at, is_deadline=False)

        task_id = self._generate_task_id()
        now = self._now()

        task = Task(
            id=task_id,
            title=title.strip(),
            description=description.strip() if description else None,
            status=TaskStatus.PENDING.value,
            priority=TaskPriority.from_str(priority).value,
            due_date=parsed_due,
            reminder_at=parsed_reminder,
            reminder_triggered=False,
            created_at=now,
            updated_at=now,
            completed_at=None,
            metadata=dict(metadata or {}),
        )

        return self._store.add_task(task)

    def get_task(self, task_id: str) -> Task:
        """Retrieve task by ID, raising TaskNotFoundError if absent."""
        if not task_id or not task_id.strip():
            raise TaskValidationError("Task ID cannot be empty.")

        task = self._store.get_task(task_id.strip())
        if task is None:
            raise TaskNotFoundError(f"Task with ID '{task_id.strip()}' not found.")
        return task

    def list_upcoming_tasks(self, now: datetime | None = None) -> list[Task]:
        """List pending tasks with future deadlines."""
        ref = now or self._now()
        tasks = self._store.list_tasks(status="pending")
        return [t for t in tasks if t.due_date is not None and t.due_date >= ref]

    def list_overdue_tasks(self, now: datetime | None = None) -> list[Task]:
        """List pending tasks whose deadline has passed."""
        ref = now or self._now()
        tasks = self._store.list_tasks(status="pending")
        return [t for t in tasks if t.due_date is not None and t.due_date < ref]

    def list_tasks_due_today(self, now: datetime | None = None) -> list[Task]:
        """List pending tasks due on today's calendar date in the local reference timezone."""
        ref = now or self._now()
        target_tz = ref.tzinfo or self._date_parser.default_tz
        ref_local = ref.astimezone(target_tz)
        today_date = ref_local.date()
        tasks = self._store.list_tasks(status="pending")
        return [
            t
            for t in tasks
            if t.due_date is not None
            and t.due_date.astimezone(target_tz).date() == today_date
        ]

    def list_tasks(
        self,
        status: str | None = None,
        priority: str | None = None,
    ) -> list[Task]:
        """List tasks matching optional status and priority criteria."""
        norm_status = status.strip().lower() if status else None
        if norm_status == "overdue":
            res = self.list_overdue_tasks()
        elif norm_status == "today":
            res = self.list_tasks_due_today()
        elif norm_status == "upcoming":
            res = self.list_upcoming_tasks()
        else:
            return self._store.list_tasks(status=status, priority=priority)

        if priority:
            p_val = priority.strip().lower()
            res = [t for t in res if t.priority.lower() == p_val]
        return res

    def search_tasks(
        self,
        query: str,
        status: str | None = None,
    ) -> list[Task]:
        """Search tasks by title or description substring."""
        if not query or not query.strip():
            return []
        return self._store.search_tasks(query.strip(), status=status)

    def update_task(
        self,
        task_id: str,
        title: str | None = None,
        description: str | None = None,
        due_date: datetime | str | None = None,
        reminder_at: datetime | str | None = None,
        priority: str | None = None,
        status: str | None = None,
        clear_reminder: bool = False,
        clear_due_date: bool = False,
    ) -> Task:
        """Update fields on an existing task.

        If reminder_at is updated, reminder_triggered is reset to False.
        Supplying 'none', 'clear', or 'cancel' clears the deadline or reminder.
        """
        existing = self.get_task(task_id)
        now = self._now()

        new_title = title.strip() if title is not None else existing.title
        if not new_title:
            raise TaskValidationError("Task title cannot be empty.")

        new_desc = (
            description.strip() if description is not None else existing.description
        )

        # Handle due date update or clearing
        if clear_due_date or (
            isinstance(due_date, str)
            and due_date.strip().lower() in {"none", "clear", "remove", "cancel"}
        ):
            new_due = None
        elif due_date is not None:
            new_due = self._parse_datetime(due_date, is_deadline=True)
        else:
            new_due = existing.due_date

        # Handle reminder update or clearing
        if clear_reminder or (
            isinstance(reminder_at, str)
            and reminder_at.strip().lower() in {"none", "clear", "remove", "cancel"}
        ):
            new_reminder = None
            new_triggered = False
        elif reminder_at is not None:
            new_reminder = self._parse_datetime(reminder_at, is_deadline=False)
            new_triggered = False
        else:
            new_reminder = existing.reminder_at
            new_triggered = existing.reminder_triggered

        new_priority = (
            TaskPriority.from_str(priority).value
            if priority is not None
            else existing.priority
        )

        new_status = (
            TaskStatus.from_str(status).value
            if status is not None
            else existing.status
        )

        completed_at = existing.completed_at
        if new_status == TaskStatus.COMPLETED.value and existing.status != TaskStatus.COMPLETED.value:
            completed_at = now
        elif new_status != TaskStatus.COMPLETED.value:
            completed_at = None

        updated_task = Task(
            id=existing.id,
            title=new_title,
            description=new_desc,
            status=new_status,
            priority=new_priority,
            due_date=new_due,
            reminder_at=new_reminder,
            reminder_triggered=new_triggered,
            created_at=existing.created_at,
            updated_at=now,
            completed_at=completed_at,
            metadata=existing.metadata,
        )

        return self._store.update_task(updated_task)

    def complete_task(self, task_id: str) -> Task:
        """Mark a task as completed."""
        return self.update_task(task_id, status=TaskStatus.COMPLETED.value)

    def cancel_task(self, task_id: str) -> Task:
        """Mark a task as cancelled."""
        return self.update_task(task_id, status=TaskStatus.CANCELLED.value)

    def delete_task(self, task_id: str) -> bool:
        """Delete a task by ID."""
        if not task_id or not task_id.strip():
            raise TaskValidationError("Task ID cannot be empty.")

        # Ensure task exists before deletion
        self.get_task(task_id)
        return self._store.delete_task(task_id.strip())

    def get_due_reminders(self, now: datetime | None = None) -> list[Task]:
        """Fetch pending tasks whose reminder instant has arrived or passed."""
        ref = now or self._now()
        return self._store.get_due_reminders(ref)

    def claim_reminder(self, task_id: str) -> bool:
        """Atomically mark a reminder as triggered."""
        if not task_id or not task_id.strip():
            return False
        return self._store.claim_reminder(task_id.strip(), self._now())

    def get_missed_reminders(self, now: datetime | None = None) -> list[Task]:
        """Retrieve missed reminders whose scheduled times passed while Anaya was closed."""
        ref = now or self._now()
        return self._store.get_missed_reminders(ref)

    def count_tasks(self, status: str | None = None) -> int:
        """Return task count."""
        return self._store.count_tasks(status=status)

    def close(self) -> None:
        """Cleanly close underlying store."""
        self._store.close()
