"""Data models for task reminders and runtime notifications."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.tasks.models import Task


@dataclass(frozen=True)
class ReminderNotification:
    """Structured payload representing a task reminder notification.

    Attributes:
        task_id: Associated task unique identifier.
        title: Task title.
        reminder_at: Scheduled reminder timestamp (timezone-aware UTC).
        description: Optional task description.
        due_date: Optional task deadline.
        priority: Task priority level (low, medium, high).
        is_missed: True if recovered after application startup from downtime.
        message: Human-readable notification message suitable for UI, logs, or TTS.
    """

    task_id: str
    title: str
    reminder_at: datetime
    description: str | None = None
    due_date: datetime | None = None
    priority: str = "medium"
    is_missed: bool = False
    message: str = ""

    def __post_init__(self) -> None:
        if not self.message:
            rem_utc = (
                self.reminder_at.astimezone(timezone.utc)
                if self.reminder_at.tzinfo
                else self.reminder_at.replace(tzinfo=timezone.utc)
            )
            time_str = rem_utc.strftime("%Y-%m-%d %H:%M UTC")
            if self.is_missed:
                msg = f"Missed reminder for task '{self.title}' (was scheduled for {time_str})."
            else:
                msg = f"Reminder: '{self.title}' is due now ({time_str})."

            if self.description:
                msg += f" Note: {self.description}"
            object.__setattr__(self, "message", msg)

    @classmethod
    def from_task(cls, task: "Task", is_missed: bool = False) -> ReminderNotification:
        """Construct a ReminderNotification from a domain Task instance."""
        rem_time = task.reminder_at or datetime.now(timezone.utc)
        return cls(
            task_id=task.id,
            title=task.title,
            reminder_at=rem_time,
            description=task.description,
            due_date=task.due_date,
            priority=task.priority,
            is_missed=is_missed,
        )
