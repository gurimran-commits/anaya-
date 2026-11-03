"""Tests for natural-language Brain and TextAssistant task integration."""

from datetime import datetime, timezone

import pytest

from app.brain.text_assistant import TextAssistant
from app.commands.executor import CommandExecutor
from app.commands.registry import CommandRegistry
from app.security.approval_service import ApprovalService
from app.tasks.task_manager import TaskManager
from app.tasks.task_store import TaskStore


@pytest.fixture
def fake_now():
    return datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def brain_assistant(tmp_path, fake_now):
    registry = CommandRegistry()
    approval = ApprovalService()
    executor = CommandExecutor(registry, approval_service=approval)
    store = TaskStore(db_path=tmp_path / "brain_tasks.db")
    task_manager = TaskManager(store=store, clock=lambda: fake_now)

    assistant = TextAssistant(
        registry=registry,
        executor=executor,
        task_manager=task_manager,
    )
    yield assistant, task_manager, approval
    task_manager.close()


class TestTaskBrain:
    """Test suite for natural language task processing and clarifications."""

    def test_natural_language_create_task_with_deadline(self, brain_assistant):
        assistant, task_manager, _ = brain_assistant

        res = assistant.process("Add a task to finish my assignment tomorrow")
        assert res.success is True
        assert "Created task: 'finish my assignment'" in res.message

        tasks = task_manager.list_tasks()
        assert len(tasks) == 1
        assert tasks[0].title == "finish my assignment"
        assert tasks[0].due_date is not None
        assert tasks[0].reminder_at is None

    def test_natural_language_create_reminder(self, brain_assistant):
        assistant, task_manager, _ = brain_assistant

        res = assistant.process("Remind me to call Divya at 5 PM")
        assert res.success is True
        assert "Created task: 'call Divya'" in res.message

        tasks = task_manager.list_tasks()
        assert len(tasks) == 1
        assert tasks[0].title == "call Divya"
        assert tasks[0].reminder_at is not None
        assert tasks[0].due_date is None

    def test_natural_language_list_pending_and_completed(self, brain_assistant):
        assistant, task_manager, _ = brain_assistant
        task_manager.create_task(title="Pending item")
        t2 = task_manager.create_task(title="Done item")
        task_manager.complete_task(t2.id)

        res_pending = assistant.process("Show my pending tasks")
        assert res_pending.success is True
        assert "Pending item" in res_pending.message
        assert "Done item" not in res_pending.message

        res_today = assistant.process("What do I need to do today?")
        assert res_today.success is True
        assert "Pending item" in res_today.message

        res_completed = assistant.process("Show my completed tasks")
        assert res_completed.success is True
        assert "Done item" in res_completed.message

    def test_natural_language_complete_by_title(self, brain_assistant):
        assistant, task_manager, _ = brain_assistant
        task = task_manager.create_task(title="finish my assignment")

        res = assistant.process("Mark my assignment as completed")
        assert res.success is True
        assert f"Completed task 'finish my assignment' (ID: {task.id})" in res.message

        updated = task_manager.get_task(task.id)
        assert updated.status == "completed"

    def test_natural_language_complete_by_id(self, brain_assistant):
        assistant, task_manager, _ = brain_assistant
        task = task_manager.create_task(title="Math exam")

        res = assistant.process(f"Complete task {task.id}")
        assert res.success is True
        assert f"Completed task 'Math exam' (ID: {task.id})" in res.message

    def test_natural_language_cancel_task(self, brain_assistant):
        assistant, task_manager, _ = brain_assistant
        task = task_manager.create_task(title="Team sync")

        res = assistant.process("Cancel the Team sync task")
        assert res.success is True
        assert f"Cancelled task 'Team sync' (ID: {task.id})" in res.message

    def test_missing_title_clarification_flow(self, brain_assistant):
        assistant, task_manager, _ = brain_assistant

        # 1. User says "Add a task" without title
        res1 = assistant.process("Add a task")
        assert res1.success is False
        assert "What task would you like to create?" in res1.message

        # Context has pending intent
        assert assistant.context.pending_intent == "tasks.create"

        # 2. User provides title
        res2 = assistant.process("buy milk and eggs")
        assert res2.success is True
        assert "Created task: 'buy milk and eggs'" in res2.message

        tasks = task_manager.list_tasks()
        assert len(tasks) == 1
        assert tasks[0].title == "buy milk and eggs"

    def test_missing_id_delete_clarification_flow(self, brain_assistant):
        assistant, task_manager, approval = brain_assistant
        task = task_manager.create_task(title="Delete me")

        # 1. User says "Delete task"
        res1 = assistant.process("Delete task")
        assert res1.success is False
        assert "Which task ID would you like to delete?" in res1.message

        # 2. User supplies ID
        res2 = assistant.process(task.id)
        assert res2.success is False
        assert "Confirmation required" in res2.message

        # 3. Spoken 'yes' must be rejected
        res_yes = assistant.process("yes")
        assert res_yes.success is False
        assert "confirmation is disabled" in res_yes.message

    def test_ambiguous_title_clarification_flow(self, brain_assistant):
        assistant, task_manager, _ = brain_assistant
        task_manager.create_task(title="Math assignment")
        task_manager.create_task(title="Physics assignment")

        res = assistant.process("Mark my assignment as completed")
        assert res.success is False
        assert "Multiple matching tasks found" in res.message
        assert "Math assignment" in res.message
        assert "Physics assignment" in res.message
