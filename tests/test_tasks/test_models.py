"""Tests for Task Manager models, enums, and validation."""

from datetime import datetime, timezone

import pytest

from app.tasks.models import (
    Task,
    TaskPriority,
    TaskStatus,
    TaskValidationError,
)


class TestTaskModels:
    """Test suite for Task, TaskStatus, and TaskPriority validation."""

    def test_valid_task_creation(self):
        now = datetime.now(timezone.utc)
        task = Task(
            id="task-1234",
            title="Complete assignment",
            description="Math homework",
            status=TaskStatus.PENDING.value,
            priority=TaskPriority.HIGH.value,
            due_date=now,
            created_at=now,
            updated_at=now,
        )

        assert task.id == "task-1234"
        assert task.title == "Complete assignment"
        assert task.description == "Math homework"
        assert task.status == TaskStatus.PENDING.value
        assert task.priority == TaskPriority.HIGH.value
        assert task.due_date == now
        assert task.reminder_triggered is False

    def test_task_immutability(self):
        task = Task(id="task-1234", title="Read book")
        with pytest.raises(Exception):
            task.title = "New title"  # type: ignore

    @pytest.mark.parametrize("empty_id", ["", "   ", "a", "ab", "abc"])
    def test_invalid_id_length(self, empty_id):
        with pytest.raises(TaskValidationError, match="Task ID"):
            Task(id=empty_id, title="Test task")

    @pytest.mark.parametrize("empty_title", ["", "   ", "  \n\t  "])
    def test_empty_title_rejected(self, empty_title):
        with pytest.raises(TaskValidationError, match="Task title must be a non-empty string"):
            Task(id="task-1234", title=empty_title)

    def test_excessive_title_length_rejected(self):
        long_title = "a" * 501
        with pytest.raises(TaskValidationError, match="exceeds maximum allowed length"):
            Task(id="task-1234", title=long_title)

    def test_excessive_description_length_rejected(self):
        long_desc = "a" * 5001
        with pytest.raises(TaskValidationError, match="exceeds maximum allowed length"):
            Task(id="task-1234", title="Test", description=long_desc)

    def test_invalid_status_rejected(self):
        with pytest.raises(TaskValidationError, match="Invalid task status"):
            Task(id="task-1234", title="Test", status="unknown_status")

    def test_invalid_priority_rejected(self):
        with pytest.raises(TaskValidationError, match="Invalid task priority"):
            Task(id="task-1234", title="Test", priority="critical")

    def test_naive_datetime_rejected(self):
        naive_dt = datetime(2026, 11, 4, 17, 0, 0)  # No tzinfo
        with pytest.raises(TaskValidationError, match="must be timezone-aware"):
            Task(id="task-1234", title="Test", due_date=naive_dt)

        with pytest.raises(TaskValidationError, match="must be timezone-aware"):
            Task(id="task-1234", title="Test", reminder_at=naive_dt)

        with pytest.raises(TaskValidationError, match="must be timezone-aware"):
            Task(id="task-1234", title="Test", completed_at=naive_dt)

    def test_to_dict_and_from_dict_roundtrip(self):
        now = datetime.now(timezone.utc)
        task = Task(
            id="task-abcd",
            title="Grocery shopping",
            description="Buy milk and bread",
            status=TaskStatus.IN_PROGRESS.value,
            priority=TaskPriority.LOW.value,
            due_date=now,
            reminder_at=now,
            reminder_triggered=True,
            metadata={"source": "voice"},
        )

        d = task.to_dict()
        assert d["id"] == "task-abcd"
        assert d["title"] == "Grocery shopping"
        assert d["reminder_triggered"] is True
        assert d["metadata"] == {"source": "voice"}

        reconstructed = Task.from_dict(d)
        assert reconstructed.id == task.id
        assert reconstructed.title == task.title
        assert reconstructed.status == task.status
        assert reconstructed.priority == task.priority
        assert reconstructed.reminder_triggered is True
        assert reconstructed.metadata == {"source": "voice"}

    def test_enum_fallback_helpers(self):
        assert TaskStatus.from_str("PENDING") == TaskStatus.PENDING
        assert TaskStatus.from_str("completed") == TaskStatus.COMPLETED
        assert TaskStatus.from_str("invalid") == TaskStatus.PENDING
        assert TaskStatus.from_str(None) == TaskStatus.PENDING  # type: ignore

        assert TaskPriority.from_str("HIGH") == TaskPriority.HIGH
        assert TaskPriority.from_str("low") == TaskPriority.LOW
        assert TaskPriority.from_str("invalid") == TaskPriority.MEDIUM

    def test_date_parser_timezone_semantics(self):
        from datetime import timedelta
        from app.tasks.date_parser import DateParser

        # Custom local timezone (e.g. UTC+05:30)
        local_tz = timezone(timedelta(hours=5, minutes=30))
        parser = DateParser(default_tz=local_tz)

        ref_local = datetime(2026, 11, 4, 12, 0, 0, tzinfo=local_tz)

        # 1. Date-only deadline represents 23:59:59 of local calendar day converted to UTC
        deadline = parser.parse("2026-11-04", reference_time=ref_local, is_deadline=True)
        assert deadline is not None
        assert deadline.tzinfo == timezone.utc
        # 23:59:59 at UTC+5:30 -> 18:29:59 UTC
        assert deadline.hour == 18
        assert deadline.minute == 29
        assert deadline.second == 59
        assert deadline.day == 4

        # 2. Date-only reminder represents 09:00:00 of local calendar day converted to UTC
        reminder = parser.parse("2026-11-04", reference_time=ref_local, is_deadline=False)
        assert reminder is not None
        assert reminder.tzinfo == timezone.utc
        # 09:00:00 at UTC+5:30 -> 03:30:00 UTC
        assert reminder.hour == 3
        assert reminder.minute == 30
        assert reminder.second == 0

        # 3. Explicit ISO timestamp with offset is normalized cleanly
        explicit = parser.parse("2026-11-04T15:00:00+02:00")
        assert explicit is not None
        assert explicit.tzinfo == timezone.utc
        # 15:00 at UTC+2:00 -> 13:00 UTC
        assert explicit.hour == 13
