"""Task Manager data models, enums, and validation exceptions."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class TaskStatus(str, Enum):
    """Lifecycle statuses for a task."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"

    @classmethod
    def from_str(cls, value: str) -> "TaskStatus":
        """Convert a string to TaskStatus safely with fallback to PENDING."""
        if not isinstance(value, str):
            return cls.PENDING
        normalized = value.strip().casefold()
        for member in cls:
            if member.value == normalized:
                return member
        return cls.PENDING


class TaskPriority(str, Enum):
    """Priority levels for a task."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @classmethod
    def from_str(cls, value: str) -> "TaskPriority":
        """Convert a string to TaskPriority safely with fallback to MEDIUM."""
        if not isinstance(value, str):
            return cls.MEDIUM
        normalized = value.strip().casefold()
        for member in cls:
            if member.value == normalized:
                return member
        return cls.MEDIUM


# ============================================================================
# Exception Hierarchy
# ============================================================================


class TaskError(Exception):
    """Base exception for all Task Manager errors."""

    pass


class TaskValidationError(TaskError):
    """Raised when task parameters or datetimes fail validation."""

    pass


class TaskNotFoundError(TaskError):
    """Raised when a specified task ID cannot be found."""

    pass


class TaskConflictError(TaskError):
    """Raised when task operations conflict or task matches are ambiguous."""

    pass


class TaskStoreError(TaskError):
    """Raised when task persistence encounters database or storage errors."""

    pass


# ============================================================================
# Task Data Model
# ============================================================================


@dataclass(frozen=True)
class Task:
    """Immutable data model representing a user task.

    All timestamps are timezone-aware and normalized to UTC when serialized.
    """

    id: str
    title: str
    description: str | None = None
    status: str = TaskStatus.PENDING.value
    priority: str = TaskPriority.MEDIUM.value
    due_date: datetime | None = None
    reminder_at: datetime | None = None
    reminder_triggered: bool = False
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    updated_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    completed_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise TaskValidationError("Task ID must be a non-empty string.")
        if len(self.id.strip()) < 4 or len(self.id.strip()) > 64:
            raise TaskValidationError(
                f"Task ID length must be between 4 and 64 characters, got {len(self.id.strip())}."
            )

        if not isinstance(self.title, str) or not self.title.strip():
            raise TaskValidationError("Task title must be a non-empty string.")
        if len(self.title) > 500:
            raise TaskValidationError(
                f"Task title exceeds maximum allowed length of 500 characters ({len(self.title)})."
            )

        if self.description is not None:
            if not isinstance(self.description, str):
                raise TaskValidationError("Task description must be a string or None.")
            if len(self.description) > 5000:
                raise TaskValidationError(
                    f"Task description exceeds maximum allowed length of 5000 characters ({len(self.description)})."
                )

        valid_statuses = {s.value for s in TaskStatus}
        if self.status not in valid_statuses:
            raise TaskValidationError(
                f"Invalid task status '{self.status}'. Must be one of: {sorted(valid_statuses)}."
            )

        valid_priorities = {p.value for p in TaskPriority}
        if self.priority not in valid_priorities:
            raise TaskValidationError(
                f"Invalid task priority '{self.priority}'. Must be one of: {sorted(valid_priorities)}."
            )

        # Datetime timezone validation: naive datetimes are strictly rejected
        if self.due_date is not None:
            if not isinstance(self.due_date, datetime):
                raise TaskValidationError("due_date must be a datetime instance or None.")
            if self.due_date.tzinfo is None or self.due_date.tzinfo.utcoffset(self.due_date) is None:
                raise TaskValidationError("due_date must be timezone-aware.")

        if self.reminder_at is not None:
            if not isinstance(self.reminder_at, datetime):
                raise TaskValidationError("reminder_at must be a datetime instance or None.")
            if self.reminder_at.tzinfo is None or self.reminder_at.tzinfo.utcoffset(self.reminder_at) is None:
                raise TaskValidationError("reminder_at must be timezone-aware.")

        if not isinstance(self.created_at, datetime) or self.created_at.tzinfo is None:
            raise TaskValidationError("created_at must be a timezone-aware datetime.")

        if not isinstance(self.updated_at, datetime) or self.updated_at.tzinfo is None:
            raise TaskValidationError("updated_at must be a timezone-aware datetime.")

        if self.completed_at is not None:
            if not isinstance(self.completed_at, datetime):
                raise TaskValidationError("completed_at must be a datetime instance or None.")
            if self.completed_at.tzinfo is None or self.completed_at.tzinfo.utcoffset(self.completed_at) is None:
                raise TaskValidationError("completed_at must be timezone-aware.")

        if not isinstance(self.metadata, dict):
            raise TaskValidationError("Task metadata must be a dictionary.")

    def to_dict(self) -> dict[str, Any]:
        """Convert task to a JSON-serializable dictionary."""
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "status": self.status,
            "priority": self.priority,
            "due_date": self.due_date.isoformat() if self.due_date else None,
            "reminder_at": self.reminder_at.isoformat() if self.reminder_at else None,
            "reminder_triggered": self.reminder_triggered,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Task":
        """Instantiate Task from a serialized dictionary."""
        due_date = None
        if data.get("due_date"):
            due_date = datetime.fromisoformat(data["due_date"])
            if due_date.tzinfo is None:
                due_date = due_date.replace(tzinfo=timezone.utc)

        reminder_at = None
        if data.get("reminder_at"):
            reminder_at = datetime.fromisoformat(data["reminder_at"])
            if reminder_at.tzinfo is None:
                reminder_at = reminder_at.replace(tzinfo=timezone.utc)

        created_at = datetime.fromisoformat(data["created_at"])
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)

        updated_at = datetime.fromisoformat(data["updated_at"])
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)

        completed_at = None
        if data.get("completed_at"):
            completed_at = datetime.fromisoformat(data["completed_at"])
            if completed_at.tzinfo is None:
                completed_at = completed_at.replace(tzinfo=timezone.utc)

        metadata = data.get("metadata", {})
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except Exception:
                metadata = {}

        return cls(
            id=data["id"],
            title=data["title"],
            description=data.get("description"),
            status=data.get("status", TaskStatus.PENDING.value),
            priority=data.get("priority", TaskPriority.MEDIUM.value),
            due_date=due_date,
            reminder_at=reminder_at,
            reminder_triggered=bool(data.get("reminder_triggered", False)),
            created_at=created_at,
            updated_at=updated_at,
            completed_at=completed_at,
            metadata=metadata if isinstance(metadata, dict) else {},
        )
