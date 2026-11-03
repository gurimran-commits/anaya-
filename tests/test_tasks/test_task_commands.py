"""Tests for task commands registration, execution, and security approvals."""

import pytest

from app.commands.executor import CommandExecutor
from app.commands.registry import CommandRegistry
from app.commands.task_commands import register_task_commands
from app.security.approval_service import ApprovalService
from app.tasks.task_manager import TaskManager
from app.tasks.task_store import TaskStore


@pytest.fixture
def test_setup(tmp_path):
    registry = CommandRegistry()
    approval = ApprovalService()
    executor = CommandExecutor(registry, approval_service=approval)
    store = TaskStore(db_path=tmp_path / "cmd_tasks.db")
    manager = TaskManager(store=store)
    register_task_commands(registry, manager)
    yield registry, executor, manager, approval
    manager.close()


class TestTaskCommands:
    """Test suite for CommandRegistry integration, validators, and approval security."""

    def test_all_task_commands_registered(self, test_setup):
        registry, _, _, _ = test_setup
        expected = [
            "tasks.create",
            "tasks.list",
            "tasks.get",
            "tasks.update",
            "tasks.complete",
            "tasks.cancel",
            "tasks.delete",
        ]
        for name in expected:
            spec = registry.get(name)
            assert spec is not None
            assert spec.name == name

    def test_create_and_list_execution(self, test_setup):
        registry, executor, manager, _ = test_setup

        res_create = executor.execute("tasks.create", ("Submit proposal", "tomorrow", "", "high"))
        assert res_create.success is True
        assert "Created task: 'Submit proposal'" in res_create.message

        res_list = executor.execute("tasks.list", ())
        assert res_list.success is True
        assert "Submit proposal" in res_list.message
        assert "high" in res_list.message.lower()

    def test_complete_and_get_execution(self, test_setup):
        registry, executor, manager, _ = test_setup
        task = manager.create_task(title="Finish feature")

        res_complete = executor.execute("tasks.complete", (task.id,))
        assert res_complete.success is True
        assert f"Completed task 'Finish feature' (ID: {task.id})" in res_complete.message

        res_get = executor.execute("tasks.get", (task.id,))
        assert res_get.success is True
        assert "Status: completed" in res_get.message

    def test_delete_requires_confirmation(self, test_setup):
        registry, executor, manager, approval = test_setup
        task = manager.create_task(title="Sensitive task")

        # Initial execution requires confirmation
        res_del = executor.execute("tasks.delete", (task.id,))
        assert res_del.success is False
        assert "confirmation" in res_del.message.lower()

        # Task is still in database
        assert manager.get_task(task.id) is not None

        # Extract confirmation ID from message
        import re
        m = re.search(r"Confirmation ID:\s*(\S+)", res_del.message)
        assert m is not None
        conf_id = m.group(1)

        # Verify approval request was created with exact arguments
        req = approval.get_request(conf_id)
        assert req is not None
        assert req.action == "tasks.delete"
        assert req.arguments == (task.id,)

        # Out-of-band approval
        approved = approval.approve(req)
        assert approved is True

        # Authorized execution succeeds
        res_approved = executor.execute("tasks.delete", (task.id,), confirmation_id=conf_id)
        assert res_approved.success is True
        assert f"Deleted task {task.id}" in res_approved.message

        # Task is now gone
        from app.tasks.models import TaskNotFoundError
        with pytest.raises(TaskNotFoundError):
            manager.get_task(task.id)

    def test_non_destructive_commands_do_not_require_confirmation(self, test_setup):
        registry, executor, manager, _ = test_setup
        spec_create = registry.get("tasks.create")
        spec_list = registry.get("tasks.list")
        spec_complete = registry.get("tasks.complete")

        assert spec_create.requires_confirmation is False
        assert spec_list.requires_confirmation is False
        assert spec_complete.requires_confirmation is False
