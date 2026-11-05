"""Register task management operations with the CommandRegistry."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.commands.registry import CommandRegistry, CommandSpec

if TYPE_CHECKING:
    from app.tasks.task_manager import TaskManager


def register_task_commands(
    registry: CommandRegistry,
    manager: TaskManager,
) -> None:
    """Register task CRUD, listing, completion, and deletion commands."""
    setattr(registry, "_task_manager", manager)
    if registry.get("tasks.create") is not None:
        return

    def create_task(*args: str) -> str:
        title = args[0]
        due_date = args[1] if len(args) > 1 and args[1].strip() else None
        reminder_at = args[2] if len(args) > 2 and args[2].strip() else None
        priority = args[3] if len(args) > 3 and args[3].strip() else "medium"

        task = manager.create_task(
            title=title,
            due_date=due_date,
            reminder_at=reminder_at,
            priority=priority,
        )

        parts = [f"Created task: '{task.title}' (ID: {task.id})"]
        if task.due_date:
            parts.append(f"Due: {task.due_date.strftime('%Y-%m-%d %H:%M UTC')}")
        if task.reminder_at:
            parts.append(f"Reminder: {task.reminder_at.strftime('%Y-%m-%d %H:%M UTC')}")
        return " | ".join(parts)

    def list_tasks(*args: str) -> str:
        status = args[0].strip() if len(args) > 0 and args[0].strip() else "pending"
        priority = args[1].strip() if len(args) > 1 and args[1].strip() else None

        tasks = manager.list_tasks(status=status, priority=priority)
        if not tasks:
            return f"No {status} tasks found."

        lines = [f"{status.capitalize()} tasks:"]
        for t in tasks:
            details = [f"- [{t.status.upper()}] {t.title} (ID: {t.id}, Priority: {t.priority}"]
            if t.due_date:
                details.append(f"Due: {t.due_date.strftime('%Y-%m-%d %H:%M UTC')}")
            if t.reminder_at:
                details.append(f"Reminder: {t.reminder_at.strftime('%Y-%m-%d %H:%M UTC')}")
            lines.append(" | ".join(details) + ")")
        return "\n".join(lines)

    def get_task(task_id: str) -> str:
        task = manager.get_task(task_id)
        lines = [
            f"Task: {task.title}",
            f"ID: {task.id}",
            f"Status: {task.status}",
            f"Priority: {task.priority}",
        ]
        if task.description:
            lines.append(f"Description: {task.description}")
        if task.due_date:
            lines.append(f"Due: {task.due_date.strftime('%Y-%m-%d %H:%M UTC')}")
        if task.reminder_at:
            lines.append(f"Reminder: {task.reminder_at.strftime('%Y-%m-%d %H:%M UTC')}")
        lines.append(f"Created: {task.created_at.strftime('%Y-%m-%d %H:%M UTC')}")
        if task.completed_at:
            lines.append(f"Completed: {task.completed_at.strftime('%Y-%m-%d %H:%M UTC')}")
        return "\n".join(lines)

    def update_task(task_id: str, *args: str) -> str:
        title = args[0] if len(args) > 0 and args[0].strip() else None
        due_date = args[1] if len(args) > 1 and args[1].strip() else None
        priority = args[2] if len(args) > 2 and args[2].strip() else None
        reminder_at = args[3] if len(args) > 3 and args[3].strip() else None

        task = manager.update_task(
            task_id=task_id,
            title=title,
            due_date=due_date,
            priority=priority,
            reminder_at=reminder_at,
        )
        parts = [f"Updated task '{task.title}' (ID: {task.id})"]
        if task.due_date:
            parts.append(f"Due: {task.due_date.strftime('%Y-%m-%d %H:%M UTC')}")
        if task.reminder_at:
            parts.append(f"Reminder: {task.reminder_at.strftime('%Y-%m-%d %H:%M UTC')}")
        elif reminder_at and reminder_at.strip().lower() in {"none", "cancel", "clear", "remove"}:
            parts.append("Reminder cleared")
        return " | ".join(parts)

    def complete_task(task_id: str) -> str:
        task = manager.complete_task(task_id)
        return f"Completed task '{task.title}' (ID: {task.id})"

    def cancel_task(task_id: str) -> str:
        task = manager.cancel_task(task_id)
        return f"Cancelled task '{task.title}' (ID: {task.id})"

    def delete_task(task_id: str) -> str:
        manager.delete_task(task_id)
        return f"Deleted task {task_id}"

    # 1. tasks.create
    registry.register(
        CommandSpec(
            name="tasks.create",
            description="Create a new task with title and optional due date or reminder",
            handler=create_task,
            validate_arguments=lambda args: len(args) >= 1 and bool(args[0].strip()),
        )
    )

    # 2. tasks.list
    registry.register(
        CommandSpec(
            name="tasks.list",
            description="List tasks filtered by status (default: pending) and priority",
            handler=list_tasks,
            validate_arguments=lambda args: len(args) <= 2,
        )
    )

    # 3. tasks.get
    registry.register(
        CommandSpec(
            name="tasks.get",
            description="Retrieve task details by ID",
            handler=get_task,
            validate_arguments=lambda args: len(args) == 1 and bool(args[0].strip()),
        )
    )

    # 4. tasks.update
    registry.register(
        CommandSpec(
            name="tasks.update",
            description="Update title, due date, or priority of an existing task",
            handler=update_task,
            validate_arguments=lambda args: len(args) >= 2 and bool(args[0].strip()),
        )
    )

    # 5. tasks.complete
    registry.register(
        CommandSpec(
            name="tasks.complete",
            description="Mark a task as completed by ID",
            handler=complete_task,
            validate_arguments=lambda args: len(args) == 1 and bool(args[0].strip()),
        )
    )

    # 6. tasks.cancel
    registry.register(
        CommandSpec(
            name="tasks.cancel",
            description="Mark a task as cancelled by ID",
            handler=cancel_task,
            validate_arguments=lambda args: len(args) == 1 and bool(args[0].strip()),
        )
    )

    # 7. tasks.delete (REQUIRES APPROVAL)
    registry.register(
        CommandSpec(
            name="tasks.delete",
            description="Delete a task by ID after explicit approval",
            handler=delete_task,
            requires_confirmation=True,
            validate_arguments=lambda args: len(args) == 1 and bool(args[0].strip()),
        )
    )
