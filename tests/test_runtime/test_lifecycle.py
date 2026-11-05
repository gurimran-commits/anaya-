"""Tests for AnayaRuntime lifecycle, thread management, and context manager."""

from __future__ import annotations

import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.core.config import AppConfig
from app.runtime import AnayaRuntime
from app.tasks.task_manager import TaskManager


@pytest.fixture
def temp_db(tmp_path):
    return tmp_path / "test_lifecycle.db"


def test_runtime_initial_state_has_no_running_threads(temp_db):
    """Runtime creation must not start any background threads before start() is called."""
    threads_before = threading.enumerate()
    runtime = AnayaRuntime(db_path=temp_db)

    assert runtime.is_running is False
    assert runtime.scheduler.is_running is False
    # No new threads spawned on construction
    threads_after = threading.enumerate()
    assert len(threads_before) == len(threads_after)
    runtime.stop()


def test_runtime_start_and_stop_lifecycle(temp_db):
    """Calling start() starts the scheduler thread, and stop() cleanly joins it."""
    runtime = AnayaRuntime(db_path=temp_db)
    try:
        runtime.start()
        assert runtime.is_running is True
        assert runtime.scheduler.is_running is True

        # Stop cleanly
        runtime.stop()
        assert runtime.is_running is False
        assert runtime.scheduler.is_running is False
    finally:
        runtime.stop()


def test_runtime_start_is_idempotent(temp_db):
    """Repeated calls to start() must not create duplicate scheduler threads."""
    runtime = AnayaRuntime(db_path=temp_db)
    try:
        runtime.start()
        initial_thread = runtime.scheduler._worker_thread
        assert initial_thread is not None
        assert initial_thread.is_alive()

        # Call start again
        runtime.start()
        runtime.start()
        assert runtime.scheduler._worker_thread is initial_thread
        assert runtime.is_running is True
    finally:
        runtime.stop()


def test_runtime_stop_is_idempotent(temp_db):
    """Repeated calls to stop() must be safe and not raise exceptions."""
    runtime = AnayaRuntime(db_path=temp_db)
    runtime.start()
    runtime.stop()
    assert runtime.is_running is False

    # Second and third stop calls
    runtime.stop()
    runtime.stop()
    assert runtime.is_running is False


def test_runtime_context_manager(temp_db):
    """Using 'with runtime:' starts and cleanly stops the runtime."""
    with AnayaRuntime(db_path=temp_db) as runtime:
        assert runtime.is_running is True
        assert runtime.scheduler.is_running is True

    # After exiting with-block, runtime is stopped
    assert runtime.is_running is False
    assert runtime.scheduler.is_running is False


def test_runtime_handles_startup_failure_gracefully(temp_db, monkeypatch):
    """If startup fails during initialization, resources are rolled back cleanly."""
    runtime = AnayaRuntime(db_path=temp_db)

    def failing_recover():
        raise RuntimeError("Simulated startup failure in recovery")

    monkeypatch.setattr(runtime, "recover_missed_reminders", failing_recover)

    with pytest.raises(RuntimeError, match="Simulated startup failure"):
        runtime.start()

    assert runtime.is_running is False
    assert runtime.scheduler.is_running is False


def test_runtime_disabled_scheduling_config(temp_db):
    """When task_scheduling_enabled is False, background thread is never started."""
    config = AppConfig(task_scheduling_enabled=False)
    runtime = AnayaRuntime(config=config, db_path=temp_db)

    try:
        runtime.start()
        assert runtime.is_running is True
        # Scheduler thread is not running
        assert runtime.scheduler.is_running is False
    finally:
        runtime.stop()


def test_runtime_restart_after_clean_stop(temp_db):
    """Runtime can be cleanly started again after being stopped."""
    runtime = AnayaRuntime(db_path=temp_db)
    try:
        # First session
        runtime.start()
        assert runtime.is_running is True
        assert runtime.scheduler.is_running is True
        t1 = runtime.task_manager.create_task("Session 1 Task")
        assert t1.id is not None
        runtime.stop()
        assert runtime.is_running is False
        assert runtime.scheduler.is_running is False

        # Restart same runtime instance
        runtime.start()
        assert runtime.is_running is True
        assert runtime.scheduler.is_running is True
        t2 = runtime.task_manager.create_task("Session 2 Task")
        assert t2.id is not None
        tasks = runtime.task_manager.list_tasks()
        assert len(tasks) == 2
        runtime.stop()
        assert runtime.is_running is False
    finally:
        runtime.stop()


def test_concurrent_start_calls_are_thread_safe(temp_db):
    """Concurrent calls to start() from multiple threads are synchronized and produce 1 worker."""
    runtime = AnayaRuntime(db_path=temp_db)
    barrier = threading.Barrier(5)
    errors: list[Exception] = []

    def start_worker():
        try:
            barrier.wait(timeout=3.0)
            runtime.start()
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=start_worker) for _ in range(5)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert len(errors) == 0
        assert runtime.is_running is True
        assert runtime.scheduler.is_running is True
        # Verify exactly one worker thread exists
        worker_threads = [
            t for t in threading.enumerate() if t.name == "AnayaTaskSchedulerWorker"
        ]
        assert len(worker_threads) == 1
    finally:
        runtime.stop()


def test_worker_alive_after_timeout_handled_safely(temp_db):
    """When a worker thread exceeds stop timeout, store is not prematurely closed."""
    entered_callback = threading.Event()
    blocker_event = threading.Event()

    def blocking_handler(notif):
        entered_callback.set()
        blocker_event.wait(timeout=2.0)

    config = AppConfig(
        task_recover_missed_reminders=False,
        task_scheduler_poll_interval=0.05,
    )
    runtime = AnayaRuntime(
        config=config,
        db_path=temp_db,
        on_reminder=blocking_handler,
    )
    try:
        # Schedule task before starting so worker picks it up on first tick
        runtime.task_manager.create_task(
            "Slow reminder",
            reminder_at=datetime.now(timezone.utc),
        )

        runtime.start()

        # Wait until worker actually enters the callback
        assert entered_callback.wait(timeout=3.0) is True

        # Calling stop with a tiny timeout will time out while worker is blocked in callback
        runtime.stop(timeout=0.01)

        # Worker is still alive, so scheduler must report is_running=True
        # and underlying store must NOT be closed while worker is running
        assert runtime.scheduler.is_running is True
        assert getattr(runtime.task_manager.store, "_closed", False) is False

        # Now unblock the worker callback
        blocker_event.set()

        # Calling stop again cleanly joins and finishes shutdown
        runtime.stop(timeout=2.0)
        assert runtime.is_running is False
        assert runtime.scheduler.is_running is False
        assert getattr(runtime.task_manager.store, "_closed", False) is True
    finally:
        blocker_event.set()
        runtime.stop()


def test_restart_during_worker_timeout_is_rejected_and_recovers_after_termination(temp_db):
    """Calling start() while previous worker is still active raises RuntimeError and recovers after exit."""
    entered_callback = threading.Event()
    blocker_event = threading.Event()

    def blocking_handler(notif):
        entered_callback.set()
        blocker_event.wait(timeout=2.0)

    config = AppConfig(
        task_recover_missed_reminders=False,
        task_scheduler_poll_interval=0.05,
    )
    runtime = AnayaRuntime(
        config=config,
        db_path=temp_db,
        on_reminder=blocking_handler,
    )
    try:
        runtime.task_manager.create_task(
            "Slow reminder",
            reminder_at=datetime.now(timezone.utc),
        )
        runtime.start()

        # Wait until worker enters callback
        assert entered_callback.wait(timeout=3.0) is True

        # Stop with tiny timeout: worker remains active
        runtime.stop(timeout=0.01)
        assert runtime.scheduler.is_running is True

        # Attempting start() while worker is still alive MUST raise RuntimeError
        with pytest.raises(RuntimeError, match="previous scheduler worker thread is still active"):
            runtime.start()

        # Database remains unclosed and safe
        assert getattr(runtime.task_manager.store, "_closed", False) is False

        # Unblock worker and wait for it to cleanly terminate
        blocker_event.set()
        import time
        for _ in range(50):
            if not runtime.scheduler.is_running:
                break
            time.sleep(0.05)
        assert runtime.scheduler.is_running is False

        # Now start() succeeds without error, starting a fresh worker thread
        runtime.start()
        assert runtime.is_running is True
        assert runtime.scheduler.is_running is True

        runtime.stop()
        assert runtime.is_running is False
        assert runtime.scheduler.is_running is False
    finally:
        blocker_event.set()
        runtime.stop()
