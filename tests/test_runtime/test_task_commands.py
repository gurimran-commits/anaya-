"""Tests for task and reminder commands, listings, updates, and natural language parsing."""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.runtime import AnayaRuntime


@pytest.fixture
def temp_db(tmp_path):
    return tmp_path / "test_commands.db"


def test_list_upcoming_overdue_and_today(temp_db):
    """Verify listing tasks by overdue, today, and upcoming filters."""
    ref_time = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
    runtime = AnayaRuntime(db_path=temp_db, clock=lambda: ref_time)

    try:
        # Overdue: due yesterday
        t_overdue = runtime.task_manager.create_task(
            "Pay electric bill",
            due_date=ref_time - timedelta(days=1),
        )
        # Today: due later today at 18:00
        t_today = runtime.task_manager.create_task(
            "Submit report",
            due_date=datetime(2026, 11, 5, 18, 0, tzinfo=timezone.utc),
        )
        # Upcoming: due next week
        t_upcoming = runtime.task_manager.create_task(
            "Doctor visit",
            due_date=ref_time + timedelta(days=7),
        )

        # 1. Overdue listing
        res_overdue = runtime.process("Show my overdue tasks")
        assert res_overdue.success is True
        assert "Pay electric bill" in res_overdue.message
        assert "Doctor visit" not in res_overdue.message

        # 2. Today listing
        res_today = runtime.process("What tasks are due today?")
        assert res_today.success is True
        assert "Submit report" in res_today.message
        assert "Doctor visit" not in res_today.message

        # 3. Upcoming listing
        res_upcoming = runtime.process("Show upcoming tasks")
        assert res_upcoming.success is True
        assert "Doctor visit" in res_upcoming.message
        assert "Pay electric bill" not in res_upcoming.message
    finally:
        runtime.stop()


def test_create_reminder_independent_of_deadline(temp_db):
    """Verify creating a reminder independently of task deadline."""
    ref_time = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
    runtime = AnayaRuntime(db_path=temp_db, clock=lambda: ref_time)

    try:
        res = runtime.process("Remind me to call Divya at 5 PM")
        assert res.success is True
        assert "Created task: 'call Divya'" in res.message
        assert "Reminder:" in res.message

        tasks = runtime.task_manager.list_tasks()
        assert len(tasks) == 1
        assert tasks[0].title == "call Divya"
        assert tasks[0].reminder_at is not None
        assert tasks[0].due_date is None  # Independent of deadline
    finally:
        runtime.stop()


def test_update_deadline_and_reminder(temp_db):
    """Verify updating a task's deadline and updating its reminder time."""
    ref_time = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
    runtime = AnayaRuntime(db_path=temp_db, clock=lambda: ref_time)

    try:
        task = runtime.task_manager.create_task(
            "Project Proposal",
            due_date="tomorrow",
        )

        # Update deadline
        res_move = runtime.process(f"Move deadline of {task.id} to Friday")
        assert res_move.success is True
        assert "Updated task" in res_move.message

        # Set / update reminder for task
        res_rem = runtime.process(f"Set a reminder for {task.id} at 4 PM")
        assert res_rem.success is True
        assert "Reminder:" in res_rem.message

        updated = runtime.task_manager.get_task(task.id)
        assert updated.reminder_at is not None
    finally:
        runtime.stop()


def test_cancel_reminder_for_task(temp_db):
    """Verify canceling the reminder for a task while keeping the task itself."""
    ref_time = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
    runtime = AnayaRuntime(db_path=temp_db, clock=lambda: ref_time)

    try:
        task = runtime.task_manager.create_task(
            "Client Presentation",
            reminder_at="5 PM",
        )
        assert task.reminder_at is not None

        # Cancel the reminder
        res_cancel = runtime.process(f"Cancel the reminder for {task.id}")
        assert res_cancel.success is True
        assert "Reminder cleared" in res_cancel.message

        reloaded = runtime.task_manager.get_task(task.id)
        assert reloaded.reminder_at is None
        assert reloaded.status == "pending"  # Task itself remains pending
    finally:
        runtime.stop()


def test_ambiguous_task_reference_triggers_clarification(temp_db):
    """When multiple tasks match a name query, return AMBIGUOUS resolution."""
    ref_time = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
    runtime = AnayaRuntime(db_path=temp_db, clock=lambda: ref_time)

    try:
        runtime.task_manager.create_task("Assignment Physics")
        runtime.task_manager.create_task("Assignment Chemistry")

        res = runtime.process("Move deadline of Assignment to tomorrow")
        assert res.success is False
        assert "Multiple matching tasks found" in res.message
    finally:
        runtime.stop()


def test_invalid_date_expression_does_not_guess_or_call_ai(temp_db):
    """Unparseable dates must return validation error instead of routing to conversational AI."""
    ref_time = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
    runtime = AnayaRuntime(db_path=temp_db, clock=lambda: ref_time)

    try:
        task = runtime.task_manager.create_task("Fix bug")
        res = runtime.process(f"Change deadline of {task.id} to somemonthinvalid")
        assert res.success is False
        assert "Could not parse" in res.message
    finally:
        runtime.stop()


def test_approval_protected_task_deletion(temp_db):
    """Deleting a task strictly requires two-step explicit approval."""
    ref_time = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
    runtime = AnayaRuntime(db_path=temp_db, clock=lambda: ref_time)

    try:
        task = runtime.task_manager.create_task("Temporary Task")

        # Request deletion: must require confirmation
        res = runtime.process(f"Delete task {task.id}")
        assert res.success is False
        assert "Confirmation required. Confirmation ID:" in res.message

        # Task still exists in database
        assert runtime.task_manager.get_task(task.id) is not None
    finally:
        runtime.stop()


def test_timezone_calendar_day_boundary_for_today(temp_db):
    """Verify 'due today' respects local calendar boundaries even when UTC day differs."""
    # Local time at UTC-5: 2026-11-05 at 23:30
    # Equivalent UTC instant: 2026-11-06 at 04:30 UTC
    tz_minus_5 = timezone(timedelta(hours=-5))
    ref_local = datetime(2026, 11, 5, 20, 0, tzinfo=tz_minus_5)
    deadline_local = datetime(2026, 11, 5, 23, 30, tzinfo=tz_minus_5)

    runtime = AnayaRuntime(db_path=temp_db, clock=lambda: ref_local)
    try:
        t = runtime.task_manager.create_task("Late Night Study", due_date=deadline_local)
        assert t.due_date is not None
        # In UTC, t.due_date is on Nov 6th
        assert t.due_date.astimezone(timezone.utc).day == 6

        # In local time (UTC-5), both ref_local and deadline_local are on Nov 5th
        due_today = runtime.task_manager.list_tasks_due_today(ref_local)
        assert len(due_today) == 1
        assert due_today[0].id == t.id

        # Natural language query must find it
        res = runtime.process("What tasks are due today?")
        assert res.success is True
        assert "Late Night Study" in res.message
    finally:
        runtime.stop()


def test_rescheduled_reminder_triggers_again(temp_db):
    """Updating a previously triggered reminder resets triggered flag and allows re-triggering."""
    delivered: list[str] = []
    now = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
    current_time = now

    runtime = AnayaRuntime(
        db_path=temp_db,
        on_reminder=lambda notif: delivered.append(notif.task_id),
        clock=lambda: current_time,
    )
    try:
        task = runtime.task_manager.create_task("Review draft", reminder_at=now)

        # 1. First trigger
        dispatched_1 = runtime.run_scheduler_pending(now=now)
        assert dispatched_1 == 1
        assert len(delivered) == 1
        reloaded = runtime.task_manager.get_task(task.id)
        assert reloaded.reminder_triggered is True

        # 2. Advance time and verify it does NOT trigger again
        later = now + timedelta(hours=1)
        dispatched_2 = runtime.run_scheduler_pending(now=later)
        assert dispatched_2 == 0

        # 3. Reschedule reminder to later time
        res_update = runtime.process(f"Set a reminder for {task.id} at 1:30 PM")
        assert res_update.success is True

        reloaded_after_update = runtime.task_manager.get_task(task.id)
        assert reloaded_after_update.reminder_triggered is False
        assert reloaded_after_update.reminder_at is not None

        # 4. Advance time to rescheduled instant and verify it triggers again
        rescheduled_time = reloaded_after_update.reminder_at
        dispatched_3 = runtime.run_scheduler_pending(now=rescheduled_time)
        assert dispatched_3 == 1
        assert len(delivered) == 2
    finally:
        runtime.stop()


def test_completed_and_cancelled_tasks_never_trigger_due_reminders(temp_db):
    """Completed and cancelled tasks are excluded from reminder queries and claims."""
    delivered: list[str] = []
    now = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)

    runtime = AnayaRuntime(
        db_path=temp_db,
        on_reminder=lambda notif: delivered.append(notif.task_id),
        clock=lambda: now,
    )
    try:
        t_completed = runtime.task_manager.create_task("Completed Task", reminder_at=now)
        runtime.task_manager.complete_task(t_completed.id)

        t_cancelled = runtime.task_manager.create_task("Cancelled Task", reminder_at=now)
        runtime.task_manager.cancel_task(t_cancelled.id)

        # Neither completed nor cancelled tasks should appear in due reminders
        due = runtime.task_manager.get_due_reminders(now)
        assert len(due) == 0

        # Neither should be claimed or dispatched
        dispatched = runtime.run_scheduler_pending(now=now)
        assert dispatched == 0
        assert len(delivered) == 0
    finally:
        runtime.stop()


def test_changing_deadline_preserves_independent_reminder(temp_db):
    """Updating a deadline leaves the independent reminder time and status completely untouched."""
    now = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
    runtime = AnayaRuntime(db_path=temp_db, clock=lambda: now)
    try:
        reminder_time = now + timedelta(hours=3)
        task = runtime.task_manager.create_task(
            "Semester Project",
            due_date=now + timedelta(days=2),
            reminder_at=reminder_time,
        )
        assert task.reminder_at == reminder_time
        assert task.reminder_triggered is False

        # Move deadline
        res = runtime.process(f"Move deadline of {task.id} to Friday")
        assert res.success is True

        updated = runtime.task_manager.get_task(task.id)
        # Reminder timestamp and triggered state are strictly preserved
        assert updated.reminder_at == reminder_time
        assert updated.reminder_triggered is False
    finally:
        runtime.stop()


def test_naive_datetime_in_create_or_update_rejected(temp_db):
    """Passing a naive datetime object directly raises TaskValidationError."""
    from app.tasks.models import TaskValidationError

    runtime = AnayaRuntime(db_path=temp_db)
    try:
        naive_dt = datetime(2026, 11, 5, 12, 0)  # No tzinfo
        with pytest.raises(TaskValidationError, match="Naive datetime objects are not accepted"):
            runtime.task_manager.create_task("Test task", due_date=naive_dt)

        t = runtime.task_manager.create_task("Valid task")
        with pytest.raises(TaskValidationError, match="Naive datetime objects are not accepted"):
            runtime.task_manager.update_task(t.id, reminder_at=naive_dt)
    finally:
        runtime.stop()


def test_date_only_deadline_and_reminder_time_policy(temp_db):
    """Date-only deadline sets 23:59:59 local; date-only reminder sets 09:00:00 local."""
    ref_time = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
    runtime = AnayaRuntime(db_path=temp_db, clock=lambda: ref_time)
    try:
        # Create task with ISO date-only deadline and reminder
        task = runtime.task_manager.create_task(
            "Taxes",
            due_date="2026-11-20",
            reminder_at="2026-11-20",
        )
        assert task.due_date is not None
        assert task.reminder_at is not None

        # Deadline is end-of-day (23:59:59)
        assert task.due_date.hour == 23
        assert task.due_date.minute == 59
        assert task.due_date.second == 59

        # Reminder is morning (09:00:00)
        assert task.reminder_at.hour == 9
        assert task.reminder_at.minute == 0
        assert task.reminder_at.second == 0
    finally:
        runtime.stop()


def test_concurrent_scheduler_ticks_cannot_deliver_duplicate(temp_db):
    """Concurrent scheduler ticks on multiple threads deliver a reminder at most once (no duplicate claims)."""
    import threading

    delivered: list[str] = []
    delivered_lock = threading.Lock()

    def on_rem(notif):
        with delivered_lock:
            delivered.append(notif.task_id)

    now = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
    runtime = AnayaRuntime(db_path=temp_db, on_reminder=on_rem, clock=lambda: now)
    try:
        task = runtime.task_manager.create_task("Atomic test task", reminder_at=now)

        barrier = threading.Barrier(5)
        results: list[int] = []
        res_lock = threading.Lock()

        def tick():
            barrier.wait(timeout=3.0)
            count = runtime.run_scheduler_pending(now=now)
            with res_lock:
                results.append(count)

        threads = [threading.Thread(target=tick) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        # Exactly 1 thread successfully claimed and dispatched the reminder
        assert sum(results) == 1
        assert len(delivered) == 1
        assert delivered[0] == task.id
    finally:
        runtime.stop()
