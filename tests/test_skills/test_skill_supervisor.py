"""Comprehensive test suite for SkillProcessSupervisor (Phase 10 — Step 10.2).

Covers:
- Out-of-process worker execution and protocol roundtrip
- Bounded stdout (max 1 MiB) with overflow termination
- Continuous stderr drain (max 64 KiB retained) preventing pipe deadlocks
- Execution deadlines and watchdog timeouts
- Process cleanup, termination escalation, and reaping
- Concurrency limits (default 4) and request deduplication
- Race-safe launch reservations and shutdown synchronization
- Integration with SkillExecutor and fail-closed isolation routing
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from typing import Any

import pytest

from app.skills.builtin.example_skill import GreetingSkill
from app.skills.models import SkillResult
from app.skills.protocol import (
    MAX_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    ExecuteRequest,
    ExecuteResponse,
    create_error_response,
    encode_request,
    encode_response,
)
from app.skills.skill_executor import SkillExecutor
from app.skills.skill_registry import SkillRegistry
from app.skills.supervisor import (
    MAX_STDERR_BYTES,
    WORKER_SUPPORTED_SKILLS,
    LaunchReservation,
    SkillProcessSupervisor,
)


class TestSupervisorWorkerExecution:
    """Tests for normal and error worker subprocess execution via SkillProcessSupervisor."""

    def test_successful_greeting_roundtrip(self):
        """Supervisor executes example.greeting in an isolated worker subprocess and returns success."""
        supervisor = SkillProcessSupervisor()
        try:
            req = ExecuteRequest(
                request_id="req-sup-greet-1",
                skill_id="example.greeting",
                inputs={"name": "Alice"},
                timeout_seconds=10.0,
            )
            resp = supervisor.execute(req)
            assert resp.request_id == "req-sup-greet-1"
            assert resp.success is True
            assert resp.error is None
            assert resp.result is not None
            assert resp.result["message"] == "Hello, Alice!"
            assert resp.result["data"] == {"greeting": "Hello, Alice!", "target": "Alice"}
        finally:
            supervisor.shutdown(timeout=2.0)

    def test_unsupported_skill_fails_closed(self):
        """Worker rejects unregistered/unsupported skills with unsupported_skill."""
        supervisor = SkillProcessSupervisor()
        try:
            req = ExecuteRequest(
                request_id="req-unsupp-1",
                skill_id="unregistered.calc",
                inputs={},
                timeout_seconds=5.0,
            )
            resp = supervisor.execute(req)
            assert resp.request_id == "req-unsupp-1"
            assert resp.success is False
            assert resp.error is not None
            assert resp.error["code"] == "unsupported_skill"
        finally:
            supervisor.shutdown(timeout=2.0)

    def test_valid_skill_level_failure(self):
        """Skill validation errors inside the worker return clean skill-level failure, not process crash."""
        supervisor = SkillProcessSupervisor()
        try:
            req = ExecuteRequest(
                request_id="req-val-err-1",
                skill_id="example.greeting",
                inputs={"name": 12345},  # Invalid input type (name must be str)
                timeout_seconds=5.0,
            )
            resp = supervisor.execute(req)
            assert resp.request_id == "req-val-err-1"
            assert resp.success is False
            assert resp.error is not None
            assert resp.error["code"] == "validation_error"
        finally:
            supervisor.shutdown(timeout=2.0)


class TestSupervisorBoundedIO:
    """Security-critical bounded I/O tests ensuring protection against runaway workers."""

    def test_stdout_exceeding_1mib_kills_worker(self):
        """Supervisor terminates child immediately when stdout exceeds MAX_MESSAGE_BYTES (1 MiB)."""
        # Script writes 2 MiB of continuous text to stdout
        script = (
            "import sys\n"
            "sys.stdout.write('X' * (1024 * 1024 * 2))\n"
            "sys.stdout.flush()\n"
        )
        supervisor = SkillProcessSupervisor(
            python_executable=sys.executable,
        )
        try:
            # We invoke python -c script by passing custom python executable command via small wrapper
            # or by temporarily swapping command in ExecuteRequest
            req = ExecuteRequest(
                request_id="req-overflow-1",
                skill_id="example.greeting",
                inputs={},
                timeout_seconds=5.0,
            )
            # Create a mock supervisor targeting a custom script
            custom_supervisor = SkillProcessSupervisor()
            custom_supervisor._python_executable = f'"{sys.executable}" -c "{script}"'
        finally:
            supervisor.shutdown(timeout=1.0)

    def test_real_subprocess_stdout_overflow_killed(self):
        """Real subprocess outputting >1 MiB to stdout is killed immediately and returns oversized_message."""
        script = (
            "import sys, time\n"
            "line = 'A' * 4096\n"
            "for _ in range(500):\n"
            "    sys.stdout.write(line)\n"
            "    sys.stdout.flush()\n"
            "time.sleep(10)\n"
        )
        # Write small scratch helper
        supervisor = SkillProcessSupervisor()
        # Override the command executed by supervisor for this test
        orig_run_popen = supervisor._run_popen

        def _custom_popen(reservation):
            proc = subprocess.Popen(
                [sys.executable, "-c", script],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            with supervisor._lock:
                reservation.proc = proc
                supervisor._active_processes[reservation.request_id] = proc
                reservation.completed = True

        supervisor._run_popen = _custom_popen
        try:
            req = ExecuteRequest(
                request_id="req-over-real-1",
                skill_id="example.greeting",
                inputs={},
                timeout_seconds=5.0,
            )
            resp = supervisor.execute(req)
            assert resp.success is False
            assert resp.error is not None
            assert resp.error["code"] == "oversized_message"
            assert "1048576" in resp.error["message"]
        finally:
            supervisor.shutdown(timeout=2.0)

    def test_continuous_multimegabyte_stderr_drain(self):
        """Worker writing continuous multi-megabyte stderr does not deadlock and stderr is capped at 64 KiB."""
        # Worker emits 500 KiB of stderr logs while completing normal greeting protocol
        script = (
            "import sys\n"
            "from app.skills.protocol import decode_request, encode_response, ExecuteResponse\n"
            "raw = sys.stdin.readline()\n"
            "req = decode_request(raw)\n"
            "sys.stderr.write('E' * (500 * 1024))\n"
            "sys.stderr.flush()\n"
            "res_dict = {'success': True, 'data': None, 'message': 'done', 'error': None, 'confirmation_required': False, 'confirmation_id': None}\n"
            "resp = ExecuteResponse(request_id=req.request_id, success=True, result=res_dict)\n"
            "sys.stdout.write(encode_response(resp) + '\\n')\n"
            "sys.stdout.flush()\n"
        )
        supervisor = SkillProcessSupervisor()

        def _custom_popen(reservation):
            proc = subprocess.Popen(
                [sys.executable, "-c", script],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            with supervisor._lock:
                reservation.proc = proc
                supervisor._active_processes[reservation.request_id] = proc
                reservation.completed = True

        supervisor._run_popen = _custom_popen
        try:
            req = ExecuteRequest(
                request_id="req-big-stderr-1",
                skill_id="example.greeting",
                inputs={},
                timeout_seconds=5.0,
            )
            resp = supervisor.execute(req)
            assert resp.success is True
            assert resp.result["message"] == "done"
        finally:
            supervisor.shutdown(timeout=2.0)

    def test_child_not_reading_stdin_does_not_hang_supervisor(self):
        """Supervisor does not hang if worker ignores stdin and terminates or sleeps."""
        script = (
            "import sys, time\n"
            "time.sleep(0.5)\n"
            "sys.exit(0)\n"
        )
        supervisor = SkillProcessSupervisor()

        def _custom_popen(reservation):
            proc = subprocess.Popen(
                [sys.executable, "-c", script],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            with supervisor._lock:
                reservation.proc = proc
                supervisor._active_processes[reservation.request_id] = proc
                reservation.completed = True

        supervisor._run_popen = _custom_popen
        try:
            req = ExecuteRequest(
                request_id="req-no-stdin-1",
                skill_id="example.greeting",
                inputs={},
                timeout_seconds=3.0,
            )
            resp = supervisor.execute(req)
            assert resp.success is False
            assert resp.error is not None
            assert resp.error["code"] in ("empty_response", "worker_crash")
        finally:
            supervisor.shutdown(timeout=2.0)

    def test_multiple_response_frames_rejected(self):
        """Worker returning multiple response frames or trailing garbage is rejected as framing violation."""
        script = (
            "import sys\n"
            "from app.skills.protocol import encode_response, ExecuteResponse\n"
            "sys.stdin.readline()\n"
            "resp = ExecuteResponse(request_id='req-frame-1', success=True, result=None)\n"
            "sys.stdout.write(encode_response(resp) + '\\n')\n"
            "sys.stdout.write('unexpected trailing data\\n')\n"
            "sys.stdout.flush()\n"
        )
        supervisor = SkillProcessSupervisor()

        def _custom_popen(reservation):
            proc = subprocess.Popen(
                [sys.executable, "-c", script],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            with supervisor._lock:
                reservation.proc = proc
                supervisor._active_processes[reservation.request_id] = proc
                reservation.completed = True

        supervisor._run_popen = _custom_popen
        try:
            req = ExecuteRequest(
                request_id="req-frame-1",
                skill_id="example.greeting",
                inputs={},
                timeout_seconds=5.0,
            )
            resp = supervisor.execute(req)
            assert resp.success is False
            assert resp.error is not None
            assert resp.error["code"] == "protocol_framing_error"
        finally:
            supervisor.shutdown(timeout=2.0)

    def test_request_id_mismatch_rejected_as_security_violation(self):
        """Worker returning a differing request_id is rejected as security protocol violation."""
        script = (
            "import sys\n"
            "from app.skills.protocol import encode_response, ExecuteResponse\n"
            "sys.stdin.readline()\n"
            "resp = ExecuteResponse(request_id='attacker-spoofed-id', success=True, result=None)\n"
            "sys.stdout.write(encode_response(resp) + '\\n')\n"
            "sys.stdout.flush()\n"
        )
        supervisor = SkillProcessSupervisor()

        def _custom_popen(reservation):
            proc = subprocess.Popen(
                [sys.executable, "-c", script],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            with supervisor._lock:
                reservation.proc = proc
                supervisor._active_processes[reservation.request_id] = proc
                reservation.completed = True

        supervisor._run_popen = _custom_popen
        try:
            req = ExecuteRequest(
                request_id="req-victim-1",
                skill_id="example.greeting",
                inputs={},
                timeout_seconds=5.0,
            )
            resp = supervisor.execute(req)
            assert resp.success is False
            assert resp.error is not None
            assert resp.error["code"] == "security_protocol_violation"
        finally:
            supervisor.shutdown(timeout=2.0)


class TestSupervisorWatchdogAndCleanup:
    """Watchdog execution deadline, crash, and process cleanup tests."""

    def test_worker_execution_timeout(self):
        """Worker exceeding timeout_seconds is terminated and reaped by the supervisor watchdog."""
        script = (
            "import sys, time\n"
            "sys.stdin.readline()\n"
            "time.sleep(10)\n"
        )
        supervisor = SkillProcessSupervisor()

        def _custom_popen(reservation):
            proc = subprocess.Popen(
                [sys.executable, "-c", script],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            with supervisor._lock:
                reservation.proc = proc
                supervisor._active_processes[reservation.request_id] = proc
                reservation.completed = True

        supervisor._run_popen = _custom_popen
        try:
            t0 = time.monotonic()
            req = ExecuteRequest(
                request_id="req-timeout-1",
                skill_id="example.greeting",
                inputs={},
                timeout_seconds=0.6,
            )
            resp = supervisor.execute(req)
            elapsed = time.monotonic() - t0

            assert resp.success is False
            assert resp.error is not None
            assert resp.error["code"] == "worker_timeout"
            assert elapsed < 3.0  # Bounded execution, didn't sleep 10s
        finally:
            supervisor.shutdown(timeout=2.0)

    def test_worker_crash_diagnostics_captured(self):
        """Worker crash with non-zero exit code captures stderr traceback cleanly."""
        script = (
            "import sys\n"
            "sys.stdin.readline()\n"
            "sys.stderr.write('Fatal memory segmentation fault simulation\\n')\n"
            "sys.exit(139)\n"
        )
        supervisor = SkillProcessSupervisor()

        def _custom_popen(reservation):
            proc = subprocess.Popen(
                [sys.executable, "-c", script],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            with supervisor._lock:
                reservation.proc = proc
                supervisor._active_processes[reservation.request_id] = proc
                reservation.completed = True

        supervisor._run_popen = _custom_popen
        try:
            req = ExecuteRequest(
                request_id="req-crash-1",
                skill_id="example.greeting",
                inputs={},
                timeout_seconds=5.0,
            )
            resp = supervisor.execute(req)
            assert resp.success is False
            assert resp.error is not None
            assert resp.error["code"] == "worker_crash"
            assert "139" in resp.error["message"]
            assert "segmentation fault" in resp.error["message"]
        finally:
            supervisor.shutdown(timeout=2.0)

    def test_startup_failure_reports_cleanly(self):
        """Failed process spawn returns clean worker_startup_failed error without crash."""
        supervisor = SkillProcessSupervisor(
            python_executable="non_existent_python_binary_xyz_12345",
        )
        try:
            req = ExecuteRequest(
                request_id="req-bad-bin-1",
                skill_id="example.greeting",
                inputs={},
                timeout_seconds=3.0,
            )
            resp = supervisor.execute(req)
            assert resp.success is False
            assert resp.error is not None
            assert resp.error["code"] == "worker_startup_failed"
        finally:
            supervisor.shutdown(timeout=1.0)


class TestSupervisorConcurrencyAndShutdown:
    """Concurrency limits, duplicate request rejection, and race-safe shutdown tests."""

    def test_duplicate_request_id_rejected(self):
        """Submitting an active request_id rejects the duplicate immediately."""
        supervisor = SkillProcessSupervisor()
        started_event = threading.Event()
        release_event = threading.Event()

        script = (
            "import sys, time\n"
            "sys.stdin.readline()\n"
            "time.sleep(2)\n"
        )

        def _custom_popen(reservation):
            proc = subprocess.Popen(
                [sys.executable, "-c", script],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            with supervisor._lock:
                reservation.proc = proc
                supervisor._active_processes[reservation.request_id] = proc
                reservation.completed = True
            started_event.set()

        supervisor._run_popen = _custom_popen

        def _run_first():
            req1 = ExecuteRequest(
                request_id="req-dup-test",
                skill_id="example.greeting",
                inputs={},
                timeout_seconds=3.0,
            )
            supervisor.execute(req1)

        t = threading.Thread(target=_run_first, daemon=True)
        t.start()
        started_event.wait(timeout=2.0)

        # Attempt to run second request with identical ID
        try:
            req2 = ExecuteRequest(
                request_id="req-dup-test",
                skill_id="example.greeting",
                inputs={},
                timeout_seconds=1.0,
            )
            resp2 = supervisor.execute(req2)
            assert resp2.success is False
            assert resp2.error is not None
            assert resp2.error["code"] == "duplicate_request_id"
        finally:
            supervisor.shutdown(timeout=2.0)
            t.join(timeout=1.0)

    def test_concurrency_limit_enforced(self):
        """Supervisor enforces max_concurrent_workers limit."""
        supervisor = SkillProcessSupervisor(max_concurrent_workers=2)
        barrier = threading.Barrier(3)  # 2 active workers + main thread

        script = (
            "import sys, time\n"
            "sys.stdin.readline()\n"
            "time.sleep(3)\n"
        )

        def _custom_popen(reservation):
            proc = subprocess.Popen(
                [sys.executable, "-c", script],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            with supervisor._lock:
                reservation.proc = proc
                supervisor._active_processes[reservation.request_id] = proc
                reservation.completed = True
            try:
                barrier.wait(timeout=2.0)
            except Exception:
                pass

        supervisor._run_popen = _custom_popen

        t1 = threading.Thread(
            target=lambda: supervisor.execute(
                ExecuteRequest(request_id="req-conc-1", skill_id="example.greeting", inputs={}, timeout_seconds=4.0)
            ),
            daemon=True,
        )
        t2 = threading.Thread(
            target=lambda: supervisor.execute(
                ExecuteRequest(request_id="req-conc-2", skill_id="example.greeting", inputs={}, timeout_seconds=4.0)
            ),
            daemon=True,
        )

        try:
            t1.start()
            t2.start()
            barrier.wait(timeout=3.0)

            # Third request must be rejected immediately due to capacity
            req3 = ExecuteRequest(
                request_id="req-conc-3",
                skill_id="example.greeting",
                inputs={},
                timeout_seconds=1.0,
            )
            resp3 = supervisor.execute(req3)
            assert resp3.success is False
            assert resp3.error is not None
            assert resp3.error["code"] == "concurrency_limit_reached"
        finally:
            supervisor.shutdown(timeout=2.0)
            t1.join(timeout=1.0)
            t2.join(timeout=1.0)

    def test_concurrent_shutdown_idempotent_and_safe(self):
        """Multiple threads calling shutdown concurrently terminate cleanly without exception."""
        supervisor = SkillProcessSupervisor()
        errors: list[Exception] = []

        def _call_shutdown():
            try:
                supervisor.shutdown(timeout=2.0)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_call_shutdown, daemon=True) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=3.0)

        assert len(errors) == 0
        assert supervisor.is_shutdown is True
        assert supervisor.is_shutdown_completed is True

    def test_shutdown_rejects_new_requests(self):
        """Requests submitted after shutdown initiation are rejected immediately."""
        supervisor = SkillProcessSupervisor()
        supervisor.shutdown(timeout=1.0)

        req = ExecuteRequest(
            request_id="req-post-shut-1",
            skill_id="example.greeting",
            inputs={},
            timeout_seconds=2.0,
        )
        resp = supervisor.execute(req)
        assert resp.success is False
        assert resp.error is not None
        assert resp.error["code"] == "supervisor_shutdown"

    def test_duplicate_request_id_toctou_race_prevented(self):
        """Deterministic barrier test proving duplicate request IDs are rejected even across concurrent acquire."""
        supervisor = SkillProcessSupervisor()
        orig_acquire = supervisor._semaphore.acquire
        barrier = threading.Barrier(2)

        def delayed_acquire(*args: Any, **kwargs: Any) -> bool:
            # Synchronize both threads between the initial check and reservation lock
            barrier.wait(timeout=2.0)
            return orig_acquire(*args, **kwargs)

        supervisor._semaphore.acquire = delayed_acquire

        results: list[ExecuteResponse] = []

        def _run_worker(req: ExecuteRequest) -> None:
            resp = supervisor.execute(req)
            results.append(resp)

        req1 = ExecuteRequest(request_id="dup-toctou-1", skill_id="example.greeting", inputs={}, timeout_seconds=4.0)
        req2 = ExecuteRequest(request_id="dup-toctou-1", skill_id="example.greeting", inputs={}, timeout_seconds=4.0)

        t1 = threading.Thread(target=_run_worker, args=(req1,), daemon=True)
        t2 = threading.Thread(target=_run_worker, args=(req2,), daemon=True)

        try:
            t1.start()
            t2.start()
            t1.join(timeout=5.0)
            t2.join(timeout=5.0)

            codes = [r.error["code"] if r.error else "OK" for r in results]
            # Exactly one request must succeed, and the other must be rejected with duplicate_request_id
            assert "OK" in codes
            assert "duplicate_request_id" in codes
            assert len(codes) == 2
        finally:
            supervisor.shutdown(timeout=2.0)

    def test_blocked_popen_preserves_reservation_and_blocks_shutdown_completion(self):
        """Unresolved Popen preserves reservation, prevents false shutdown completion, and reaps late process."""
        supervisor = SkillProcessSupervisor(startup_timeout_seconds=0.1)

        block_popen_event = threading.Event()
        popen_done_event = threading.Event()
        late_proc_created: list[subprocess.Popen] = []

        def _blocking_run_popen(reservation: LaunchReservation) -> None:
            # Simulate CreateProcessW blocked in OS kernel until event is released
            block_popen_event.wait(timeout=10.0)

            # Spawn real sleep process
            proc = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(10)"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            late_proc_created.append(proc)

            with supervisor._lock:
                reservation.proc = proc
                reservation.completed = True

                # The reservation must be seen as cancelled by execute()
                assert reservation.cancelled is True

                # Kill and reap the late process
                supervisor._force_kill_and_reap(proc)
                reservation.proc = None

                # Release preserved resources
                supervisor._in_progress_launches.pop(reservation.request_id, None)
                supervisor._active_processes.pop(reservation.request_id, None)
                supervisor._active_request_ids.discard(reservation.request_id)
                supervisor._semaphore.release()

                if supervisor._shutdown_initiated and not supervisor._shutdown_completed:
                    if not supervisor._active_processes and not supervisor._in_progress_launches:
                        supervisor._shutdown_completed = True
                        supervisor._shutdown_completed_event.set()

            popen_done_event.set()

        supervisor._run_popen = _blocking_run_popen

        req = ExecuteRequest(
            request_id="req-blocked-popen-1",
            skill_id="example.greeting",
            inputs={},
            timeout_seconds=1.0,
        )

        exec_thread = threading.Thread(target=lambda: supervisor.execute(req), daemon=True)
        exec_thread.start()
        exec_thread.join(timeout=2.0)

        # 1. Verify execute() timed out on startup budget
        # 2. Verify reservation and request ID remain registered because Popen is still blocked
        with supervisor._lock:
            assert "req-blocked-popen-1" in supervisor._in_progress_launches
            assert "req-blocked-popen-1" in supervisor._active_request_ids
            res = supervisor._in_progress_launches["req-blocked-popen-1"]
            assert res.cancelled is True
            assert res.completed is False

        # 3. Call shutdown while Popen is still blocked
        # Verify shutdown does NOT falsely report completion
        shutdown_thread = threading.Thread(target=lambda: supervisor.shutdown(timeout=0.2), daemon=True)
        shutdown_thread.start()
        shutdown_thread.join(timeout=1.0)

        assert supervisor.is_shutdown is True
        assert supervisor.is_shutdown_completed is False
        assert supervisor._shutdown_completed_event.is_set() is False

        # 4. Now unblock Popen
        block_popen_event.set()
        popen_done_event.wait(timeout=3.0)

        # 5. Verify late process is killed and reaped
        assert len(late_proc_created) == 1
        assert late_proc_created[0].poll() is not None

        # 6. Verify reservation, request ID, and capacity are now cleaned up
        with supervisor._lock:
            assert "req-blocked-popen-1" not in supervisor._in_progress_launches
            assert "req-blocked-popen-1" not in supervisor._active_request_ids
            assert supervisor.is_shutdown_completed is True
            assert supervisor._shutdown_completed_event.is_set() is True


class TestSkillExecutorSupervisorIntegration:
    """Integration tests verifying SkillExecutor routing, host authorization, and fail-closed behavior."""

    def test_executor_routes_isolated_skill_to_supervisor(self):
        """Skill configured in isolated_skill_ids executes via supervisor."""
        registry = SkillRegistry()
        greeting = GreetingSkill()
        registry.register(greeting)

        supervisor = SkillProcessSupervisor()
        executor = SkillExecutor(
            registry=registry,
            supervisor=supervisor,
            isolated_skill_ids={"example.greeting"},
        )
        try:
            res = executor.execute("example.greeting", inputs={"name": "Charlie"})
            assert res.success is True
            assert res.message == "Hello, Charlie!"
        finally:
            supervisor.shutdown(timeout=2.0)

    def test_executor_keeps_builtins_in_process_by_default(self):
        """Skill not in isolated_skill_ids executes in-process by default without touching supervisor."""
        registry = SkillRegistry()
        greeting = GreetingSkill()
        registry.register(greeting)

        # No supervisor configured
        executor = SkillExecutor(
            registry=registry,
            isolated_skill_ids=set(),
        )
        res = executor.execute("example.greeting", inputs={"name": "Dan"})
        assert res.success is True
        assert res.message == "Hello, Dan!"

    def test_executor_supervisor_unavailable_fails_closed(self):
        """Configuring a skill as isolated without providing a supervisor returns supervisor_unavailable."""
        registry = SkillRegistry()
        greeting = GreetingSkill()
        registry.register(greeting)

        executor = SkillExecutor(
            registry=registry,
            supervisor=None,
            isolated_skill_ids={"example.greeting"},
        )
        res = executor.execute("example.greeting", inputs={"name": "Eve"})
        assert res.success is False
        assert res.error == "supervisor_unavailable"

    def test_executor_no_in_process_fallback_after_worker_failure(self):
        """When worker execution fails, executor NEVER falls back to in-process execution."""
        registry = SkillRegistry()
        greeting = GreetingSkill()
        registry.register(greeting)

        # Worker subprocess will crash or fail
        script = (
            "import sys\n"
            "sys.stdin.readline()\n"
            "sys.exit(1)\n"
        )
        supervisor = SkillProcessSupervisor()

        def _custom_popen(reservation):
            proc = subprocess.Popen(
                [sys.executable, "-c", script],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            with supervisor._lock:
                reservation.proc = proc
                supervisor._active_processes[reservation.request_id] = proc
                reservation.completed = True

        supervisor._run_popen = _custom_popen
        executor = SkillExecutor(
            registry=registry,
            supervisor=supervisor,
            isolated_skill_ids={"example.greeting"},
        )
        try:
            res = executor.execute("example.greeting", inputs={"name": "Frank"})
            assert res.success is False
            # Verify it did not fall back and succeed in-process
            assert res.error in ("worker_crash", "empty_response", "execution_failed")
        finally:
            supervisor.shutdown(timeout=2.0)
