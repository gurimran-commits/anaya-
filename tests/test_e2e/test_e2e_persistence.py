"""End-to-end persistence validation for Anaya SQLite storage across application restarts."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from app.memory.models import MemoryCategory
from app.tasks.models import TaskNotFoundError, TaskPriority, TaskStatus
from tests.test_e2e.conftest import ControllerFactory


def test_memory_persistence_across_restarts(e2e_controller_factory: ControllerFactory) -> None:
    """Scenario 1: Memories saved in one session are accurately retrieved in a new session."""
    # Session 1: Create and save a memory with tags and metadata
    ctrl1 = e2e_controller_factory()
    ctrl1.start()

    created_mem = ctrl1.memory_manager.save_memory(
        content="Primary backup key located in secure vault sector 7",
        category=MemoryCategory.FACT,
        tags=("backup", "vault", "security"),
        metadata={"clearance_level": 4, "automated_sync": True},
    )
    assert created_mem.id is not None
    mem_id = created_mem.id
    ctrl1.stop(timeout=2.0)

    # Session 2: Create a separate controller pointing to the same memory database
    ctrl2 = e2e_controller_factory()
    ctrl2.start()

    retrieved_mem = ctrl2.memory_manager.get_memory(mem_id)
    assert retrieved_mem is not None
    assert retrieved_mem.id == mem_id
    assert retrieved_mem.content == "Primary backup key located in secure vault sector 7"
    assert retrieved_mem.category == MemoryCategory.FACT.value
    assert retrieved_mem.tags == ("backup", "vault", "security")
    assert retrieved_mem.metadata == {"clearance_level": 4, "automated_sync": True}
    assert retrieved_mem.created_at == created_mem.created_at
    assert retrieved_mem.created_at.tzinfo is not None

    ctrl2.stop(timeout=2.0)


def test_memory_update_and_delete_persistence_across_restarts(
    e2e_controller_factory: ControllerFactory,
) -> None:
    """Scenario 2: Memory updates and deletions survive separate application restart cycles."""
    # Session 1: Create initial memory, then update it
    ctrl1 = e2e_controller_factory()
    ctrl1.start()

    initial_mem = ctrl1.memory_manager.save_memory(
        content="Draft project documentation v1",
        category=MemoryCategory.PROJECT,
    )
    mem_id = initial_mem.id

    updated_mem = ctrl1.memory_manager.update_memory(
        memory_id=mem_id,
        content="Finalized project documentation v2",
        category=MemoryCategory.FACT,
        tags=("docs", "v2"),
        metadata={"version": 2, "reviewed": True},
    )
    assert updated_mem is not None
    ctrl1.stop(timeout=2.0)

    # Session 2: Verify update persisted across restart, then delete the memory
    ctrl2 = e2e_controller_factory()
    ctrl2.start()

    mem_s2 = ctrl2.memory_manager.get_memory(mem_id)
    assert mem_s2 is not None
    assert mem_s2.content == "Finalized project documentation v2"
    assert mem_s2.category == MemoryCategory.FACT.value
    assert mem_s2.tags == ("docs", "v2")
    assert mem_s2.metadata == {"version": 2, "reviewed": True}

    delete_success = ctrl2.memory_manager.delete_memory(mem_id)
    assert delete_success is True
    ctrl2.stop(timeout=2.0)

    # Session 3: Verify deletion is durable across another restart
    ctrl3 = e2e_controller_factory()
    ctrl3.start()

    assert ctrl3.memory_manager.get_memory(mem_id) is None
    all_memories = ctrl3.memory_manager.list_memories()
    assert not any(m.id == mem_id for m in all_memories)

    ctrl3.stop(timeout=2.0)


def test_task_persistence_across_restarts(e2e_controller_factory: ControllerFactory) -> None:
    """Scenario 3: Tasks created in one session persist accurately into subsequent sessions."""
    # Session 1: Create task with due date, priority, and description
    ctrl1 = e2e_controller_factory()
    ctrl1.start()

    task1 = ctrl1.task_manager.create_task(
        title="Deploy Anaya Production Cluster",
        due_date=datetime(2026, 12, 1, 15, 30, tzinfo=timezone.utc),
        priority=TaskPriority.HIGH,
        description="End-to-end production deployment check",
    )
    assert task1.id is not None
    task_id = task1.id
    ctrl1.stop(timeout=2.0)

    # Session 2: Recreate controller against the same task database
    ctrl2 = e2e_controller_factory()
    ctrl2.start()

    task2 = ctrl2.task_manager.get_task(task_id)
    assert task2 is not None
    assert task2.id == task_id
    assert task2.title == "Deploy Anaya Production Cluster"
    assert task2.description == "End-to-end production deployment check"
    assert task2.priority == TaskPriority.HIGH.value
    assert task2.status == TaskStatus.PENDING.value
    assert task2.due_date == datetime(2026, 12, 1, 15, 30, tzinfo=timezone.utc)
    assert task2.created_at == task1.created_at
    assert task2.created_at.tzinfo is not None

    ctrl2.stop(timeout=2.0)


def test_task_state_transitions_persist_across_restarts(
    e2e_controller_factory: ControllerFactory,
) -> None:
    """Scenario 4: Task status changes (completed, cancelled) and property updates persist across restarts."""
    # Session 1: Create two tasks, complete the first task
    ctrl1 = e2e_controller_factory()
    ctrl1.start()

    task_a = ctrl1.task_manager.create_task(title="Task A - Target for Completion")
    task_b = ctrl1.task_manager.create_task(title="Task B - Target for Cancellation")

    completed_a = ctrl1.task_manager.complete_task(task_a.id)
    assert completed_a.status == TaskStatus.COMPLETED.value
    assert completed_a.completed_at is not None
    completed_at_a = completed_a.completed_at

    ctrl1.stop(timeout=2.0)

    # Session 2: Verify completion persisted, cancel task B, and update task title
    ctrl2 = e2e_controller_factory()
    ctrl2.start()

    task_a_s2 = ctrl2.task_manager.get_task(task_a.id)
    assert task_a_s2.status == TaskStatus.COMPLETED.value
    assert task_a_s2.completed_at == completed_at_a

    cancelled_b = ctrl2.task_manager.cancel_task(task_b.id)
    assert cancelled_b.status == TaskStatus.CANCELLED.value

    updated_b = ctrl2.task_manager.update_task(
        task_id=task_b.id,
        title="Task B - Cancelled and Renamed",
        priority=TaskPriority.LOW,
    )
    assert updated_b.title == "Task B - Cancelled and Renamed"
    assert updated_b.priority == TaskPriority.LOW.value

    ctrl2.stop(timeout=2.0)

    # Session 3: Verify all status and property changes survive across restart
    ctrl3 = e2e_controller_factory()
    ctrl3.start()

    task_b_s3 = ctrl3.task_manager.get_task(task_b.id)
    assert task_b_s3.status == TaskStatus.CANCELLED.value
    assert task_b_s3.title == "Task B - Cancelled and Renamed"
    assert task_b_s3.priority == TaskPriority.LOW.value

    ctrl3.stop(timeout=2.0)


def test_database_isolation_between_distinct_database_paths(
    e2e_controller_factory: ControllerFactory,
    tmp_path: Path,
) -> None:
    """Scenario 5: Separate database paths do not share task or memory data."""
    dir_a = tmp_path / "env_a"
    dir_b = tmp_path / "env_b"
    dir_a.mkdir(parents=True, exist_ok=True)
    dir_b.mkdir(parents=True, exist_ok=True)

    ctrl_a = e2e_controller_factory(
        tasks_db_path=dir_a / "tasks.db",
        memory_db_path=dir_a / "memory.db",
    )
    ctrl_b = e2e_controller_factory(
        tasks_db_path=dir_b / "tasks.db",
        memory_db_path=dir_b / "memory.db",
    )

    ctrl_a.start()
    ctrl_b.start()

    # Create task and memory in environment A
    task_a = ctrl_a.task_manager.create_task(title="Exclusive Task in Env A")
    mem_a = ctrl_a.memory_manager.save_memory(content="Exclusive Memory in Env A")

    # Environment B must be completely isolated
    assert ctrl_b.task_manager.count_tasks() == 0
    assert len(ctrl_b.memory_manager.list_memories()) == 0

    with pytest.raises(TaskNotFoundError):
        ctrl_b.task_manager.get_task(task_a.id)

    assert ctrl_b.memory_manager.get_memory(mem_a.id) is None

    ctrl_a.stop(timeout=2.0)
    ctrl_b.stop(timeout=2.0)


def test_data_integrity_and_serialization_survival(
    e2e_controller_factory: ControllerFactory,
) -> None:
    """Scenario 6: Complex metadata, unicode content, and timezone-aware timestamps survive serialization roundtrip."""
    ctrl1 = e2e_controller_factory()
    ctrl1.start()

    complex_metadata: dict[str, Any] = {
        "nested": {"level1": {"level2": "deep_value"}},
        "int_list": [10, 20, 30],
        "bool_val": True,
        "null_val": None,
        "unicode": "Anaya 🌸 智能助手",
    }

    # Create memory with rich unicode content and complex nested metadata
    mem1 = ctrl1.memory_manager.save_memory(
        content="User preferences: dark mode 🌙, language: 日本語, font-size: 14px",
        category=MemoryCategory.PREFERENCE,
        tags=("ui", "theme", "i18n"),
        metadata=complex_metadata,
    )

    # Create task with timezone-aware reminder and deadline
    task1 = ctrl1.task_manager.create_task(
        title="Verify Internationalization & Localization 🌐",
        description="Comprehensive UTF-8 and JSON serialization verification across SQLite WAL checkpoints",
        due_date=datetime(2026, 11, 30, 23, 59, 59, tzinfo=timezone.utc),
        reminder_at=datetime(2026, 11, 30, 12, 0, 0, tzinfo=timezone.utc),
        priority=TaskPriority.HIGH,
    )

    mem_id = mem1.id
    task_id = task1.id
    ctrl1.stop(timeout=2.0)

    # Restart in a completely separate controller
    ctrl2 = e2e_controller_factory()
    ctrl2.start()

    reloaded_mem = ctrl2.memory_manager.get_memory(mem_id)
    assert reloaded_mem is not None
    assert reloaded_mem.content == "User preferences: dark mode 🌙, language: 日本語, font-size: 14px"
    assert reloaded_mem.category == MemoryCategory.PREFERENCE.value
    assert reloaded_mem.tags == ("ui", "theme", "i18n")
    assert reloaded_mem.metadata == complex_metadata
    assert reloaded_mem.created_at.tzinfo == timezone.utc

    reloaded_task = ctrl2.task_manager.get_task(task_id)
    assert reloaded_task is not None
    assert reloaded_task.title == "Verify Internationalization & Localization 🌐"
    assert reloaded_task.description == "Comprehensive UTF-8 and JSON serialization verification across SQLite WAL checkpoints"
    assert reloaded_task.due_date == datetime(2026, 11, 30, 23, 59, 59, tzinfo=timezone.utc)
    assert reloaded_task.reminder_at == datetime(2026, 11, 30, 12, 0, 0, tzinfo=timezone.utc)
    assert reloaded_task.priority == TaskPriority.HIGH.value
    assert reloaded_task.reminder_triggered is False

    ctrl2.stop(timeout=2.0)
