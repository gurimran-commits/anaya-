"""Process supervisor for managing isolated, one-shot skill worker subprocesses.

This module provides SkillProcessSupervisor, responsible for launching, monitoring,
bounding, and cleanly terminating isolated worker child processes over standard I/O pipes.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
import subprocess
import sys
import threading
import time
from typing import TextIO

from app.skills.protocol import (
    MAX_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    ExecuteRequest,
    ExecuteResponse,
    ProtocolError,
    create_error_response,
    decode_response,
    encode_request,
)

logger = logging.getLogger("anaya.skills.supervisor")

# Configuration Constants
DEFAULT_MAX_CONCURRENT_WORKERS: int = 4
DEFAULT_STARTUP_TIMEOUT_SECONDS: float = 3.0
DEFAULT_TRANSMISSION_TIMEOUT_SECONDS: float = 2.0
DEFAULT_CLEANUP_GRACE_PERIOD_SECONDS: float = 1.0
MAX_STDERR_BYTES: int = 65_536  # 64 KiB diagnostic log retention ceiling
PIPE_CHUNK_SIZE: int = 4096

# Explicit allowlist of skills known to be supported by the isolated worker registry
# in Step 10.2. Must match app.skills.worker and builtin skill registrations.
WORKER_SUPPORTED_SKILLS: frozenset[str] = frozenset({"example.greeting"})


class SupervisorError(Exception):
    """Base exception for all supervisor errors."""


class SupervisorShutdownError(SupervisorError):
    """Raised when an operation is attempted while the supervisor is shutting down or stopped."""


@dataclass
class LaunchReservation:
    """Tracks in-progress process launch state to prevent untracked orphan subprocesses."""

    request_id: str
    deadline: float
    proc: subprocess.Popen | None = None
    error: Exception | None = None
    cancelled: bool = False
    completed: bool = False


class SkillProcessSupervisor:
    """Manages the lifecycle, communication, watchdog timeouts, and cleanup of one-shot skill worker subprocesses."""

    def __init__(
        self,
        python_executable: str | None = None,
        max_concurrent_workers: int = DEFAULT_MAX_CONCURRENT_WORKERS,
        startup_timeout_seconds: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
        transmission_timeout_seconds: float = DEFAULT_TRANSMISSION_TIMEOUT_SECONDS,
        cleanup_grace_period_seconds: float = DEFAULT_CLEANUP_GRACE_PERIOD_SECONDS,
    ) -> None:
        self._python_executable = python_executable or sys.executable
        self._max_concurrent_workers = max_concurrent_workers
        self._startup_timeout_seconds = startup_timeout_seconds
        self._transmission_timeout_seconds = transmission_timeout_seconds
        self._cleanup_grace_period_seconds = cleanup_grace_period_seconds

        self._lock = threading.RLock()
        self._semaphore = threading.BoundedSemaphore(max_concurrent_workers)

        # Process and request tracking
        self._active_request_ids: set[str] = set()
        self._active_processes: dict[str, subprocess.Popen] = {}
        self._in_progress_launches: dict[str, LaunchReservation] = {}

        # Shutdown state
        self._shutdown_initiated = False
        self._shutdown_completed = False
        self._shutdown_completed_event = threading.Event()

    @property
    def is_shutdown(self) -> bool:
        """True if shutdown has been initiated."""
        with self._lock:
            return self._shutdown_initiated

    @property
    def is_shutdown_completed(self) -> bool:
        """True if all child processes and reservations have completed cleanup."""
        with self._lock:
            return self._shutdown_completed

    @property
    def active_worker_count(self) -> int:
        """Number of active processes currently executing."""
        with self._lock:
            return len(self._active_processes)

    def is_supported(self, skill_id: str) -> bool:
        """Verify whether a skill ID is in the explicit worker-supported allowlist."""
        return skill_id in WORKER_SUPPORTED_SKILLS

    def execute(self, request: ExecuteRequest) -> ExecuteResponse:
        """Execute a validated ExecuteRequest in a fresh, isolated child subprocess.

        Guarantees:
            - Bounded stdout (max 1 MiB) with immediate termination on overflow.
            - Bounded stderr (max 64 KiB retained) with continuous non-deadlocking drain.
            - Monotonic execution deadline enforcement covering startup, transmission, execution, and cleanup.
            - Atomic launch reservations preventing orphan processes under slow startup or shutdown.
            - Capacity limit enforcement and request ID deduplication.
            - Guaranteed cleanup and process reaping in all scenarios.
        """
        start_time = time.monotonic()
        total_timeout = float(request.timeout_seconds)
        absolute_deadline = start_time + total_timeout

        # 1. Verify supervisor lifecycle state
        with self._lock:
            if self._shutdown_initiated:
                return create_error_response(
                    request.request_id,
                    "supervisor_shutdown",
                    "SkillProcessSupervisor is shutting down or has shut down.",
                )
            if request.request_id in self._active_request_ids:
                return create_error_response(
                    request.request_id,
                    "duplicate_request_id",
                    f"Request ID '{request.request_id}' is already active in supervisor.",
                )

        # 2. Acquire concurrency capacity slot
        acquired = self._semaphore.acquire(blocking=False)
        if not acquired:
            logger.warning(
                "Execution request '%s' rejected: concurrency limit (%d) reached.",
                request.request_id,
                self._max_concurrent_workers,
            )
            return create_error_response(
                request.request_id,
                "concurrency_limit_reached",
                f"Maximum concurrent worker limit ({self._max_concurrent_workers}) reached.",
            )

        reservation = LaunchReservation(
            request_id=request.request_id,
            deadline=absolute_deadline,
        )

        with self._lock:
            if self._shutdown_initiated:
                self._semaphore.release()
                return create_error_response(
                    request.request_id,
                    "supervisor_shutdown",
                    "SkillProcessSupervisor is shutting down or has shut down.",
                )
            if request.request_id in self._active_request_ids:
                self._semaphore.release()
                return create_error_response(
                    request.request_id,
                    "duplicate_request_id",
                    f"Request ID '{request.request_id}' is already active in supervisor.",
                )
            self._active_request_ids.add(request.request_id)
            self._in_progress_launches[request.request_id] = reservation

        proc: subprocess.Popen | None = None
        try:
            # 3. Launch process with bounded startup coordination
            proc = self._launch_worker_process(reservation, absolute_deadline)
            if proc is None:
                error_msg = str(reservation.error or "Process startup failed or timed out.")
                return create_error_response(
                    request.request_id,
                    "worker_startup_failed",
                    f"Failed to spawn skill worker: {error_msg}",
                )

            # 4. Perform bounded bidirectional I/O with absolute watchdog deadline
            return self._communicate_bounded(proc, request, absolute_deadline)

        finally:
            self._cleanup_execution(request.request_id, proc, reservation)

    def _launch_worker_process(
        self,
        reservation: LaunchReservation,
        deadline: float,
    ) -> subprocess.Popen | None:
        """Launch worker subprocess in a launcher thread to bound synchronous Popen/CreateProcessW."""
        startup_budget = min(self._startup_timeout_seconds, max(0.1, deadline - time.monotonic()))
        startup_deadline = time.monotonic() + startup_budget

        launcher_thread = threading.Thread(
            target=self._run_popen,
            args=(reservation,),
            name=f"skill-launcher-{reservation.request_id}",
            daemon=True,
        )
        launcher_thread.start()

        rem_startup = max(0.0, startup_deadline - time.monotonic())
        launcher_thread.join(timeout=rem_startup)

        with self._lock:
            if not reservation.completed:
                # Startup budget exceeded while in CreateProcessW
                logger.error(
                    "Worker startup timed out after %.2fs for request '%s'.",
                    startup_budget,
                    reservation.request_id,
                )
                reservation.cancelled = True
                reservation.error = TimeoutError(f"Worker startup exceeded {startup_budget:.2f}s budget.")
                return None

            if reservation.cancelled or self._shutdown_initiated or time.monotonic() >= deadline:
                reservation.cancelled = True
                if reservation.proc is not None:
                    self._force_kill_and_reap(reservation.proc)
                    reservation.proc = None
                return None

            return reservation.proc

    def _run_popen(self, reservation: LaunchReservation) -> None:
        """Thread worker executing subprocess.Popen and registering the handle."""
        proc: subprocess.Popen | None = None
        spawn_error: Exception | None = None
        try:
            proc = subprocess.Popen(
                [self._python_executable, "-m", "app.skills.worker"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except Exception as exc:
            spawn_error = exc

        with self._lock:
            reservation.proc = proc
            reservation.error = spawn_error
            reservation.completed = True

            if reservation.cancelled or self._shutdown_initiated or time.monotonic() >= reservation.deadline:
                # Late-created process or cancelled launch: kill and reap immediately
                if proc is not None:
                    self._force_kill_and_reap(proc)
                    reservation.proc = None

                # Release the preserved reservation, request ID, and capacity
                self._in_progress_launches.pop(reservation.request_id, None)
                self._active_processes.pop(reservation.request_id, None)
                self._active_request_ids.discard(reservation.request_id)
                self._semaphore.release()

                # If shutdown was initiated and all active processes and launch reservations are now resolved
                if self._shutdown_initiated and not self._shutdown_completed:
                    if not self._active_processes and not self._in_progress_launches:
                        self._shutdown_completed = True
                        self._shutdown_completed_event.set()
                return

            self._active_processes[reservation.request_id] = proc

    def _communicate_bounded(
        self,
        proc: subprocess.Popen,
        request: ExecuteRequest,
        deadline: float,
    ) -> ExecuteResponse:
        """Perform strictly bounded I/O: drain stdout/stderr, enforce byte limits and monotonic timeout."""
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        stdout_bytes_collected = 0
        stderr_bytes_collected = 0

        stdout_overflow = threading.Event()
        io_error_holder: list[Exception] = []

        # 1. Stdout reader thread: bounded to MAX_MESSAGE_BYTES
        def _read_stdout() -> None:
            nonlocal stdout_bytes_collected
            try:
                pipe = proc.stdout
                if pipe is None:
                    return
                while True:
                    chunk = pipe.read(PIPE_CHUNK_SIZE)
                    if not chunk:
                        break
                    stdout_bytes_collected += len(chunk)
                    if stdout_bytes_collected > MAX_MESSAGE_BYTES:
                        stdout_overflow.set()
                        # Kill immediately on stdout overflow to stop runaway memory allocation
                        try:
                            proc.kill()
                        except Exception:
                            pass
                        break
                    stdout_chunks.append(chunk)
            except Exception as exc:
                io_error_holder.append(exc)

        # 2. Stderr reader thread: continuous drain, retain max 64 KiB
        def _read_stderr() -> None:
            nonlocal stderr_bytes_collected
            try:
                pipe = proc.stderr
                if pipe is None:
                    return
                while True:
                    chunk = pipe.read(PIPE_CHUNK_SIZE)
                    if not chunk:
                        break
                    if stderr_bytes_collected < MAX_STDERR_BYTES:
                        retain_slice = chunk[: MAX_STDERR_BYTES - stderr_bytes_collected]
                        stderr_chunks.append(retain_slice)
                        stderr_bytes_collected += len(retain_slice)
                    # Discard excess bytes to continuously drain pipe and prevent child deadlock
            except Exception as exc:
                io_error_holder.append(exc)

        stdout_thread = threading.Thread(
            target=_read_stdout,
            name=f"stdout-reader-{request.request_id}",
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_read_stderr,
            name=f"stderr-reader-{request.request_id}",
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()

        # 3. Transmit request payload to stdin in dedicated writer thread
        transmission_budget = min(self._transmission_timeout_seconds, max(0.1, deadline - time.monotonic()))
        stdin_done = threading.Event()
        stdin_error: list[Exception] = []

        def _write_stdin() -> None:
            try:
                pipe = proc.stdin
                if pipe is None:
                    return
                raw_payload = (encode_request(request) + "\n").encode("utf-8")
                pipe.write(raw_payload)
                pipe.flush()
                pipe.close()
            except Exception as exc:
                stdin_error.append(exc)
            finally:
                stdin_done.set()

        writer_thread = threading.Thread(
            target=_write_stdin,
            name=f"stdin-writer-{request.request_id}",
            daemon=True,
        )
        writer_thread.start()

        writer_ok = stdin_done.wait(timeout=transmission_budget)
        if not writer_ok:
            logger.error("Stdin transmission timed out for request '%s'.", request.request_id)
            self._force_kill_and_reap(proc)
            self._join_threads(stdout_thread, stderr_thread, writer_thread)
            return create_error_response(
                request.request_id,
                "worker_io_timeout",
                f"Failed to transmit request to worker stdin within {transmission_budget:.1f}s.",
            )

        if stdin_error:
            logger.error("Stdin write error for request '%s': %s", request.request_id, stdin_error[0])
            self._force_kill_and_reap(proc)
            self._join_threads(stdout_thread, stderr_thread, writer_thread)
            return create_error_response(
                request.request_id,
                "pipe_write_error",
                f"Stdin write error: {stdin_error[0]}",
            )

        # 4. Wait boundedly for process exit
        remaining_timeout = max(0.0, deadline - time.monotonic())
        timed_out = False
        try:
            proc.wait(timeout=remaining_timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            logger.warning(
                "Execution timeout (%.2fs) expired for skill '%s' (request '%s'). Killing worker.",
                request.timeout_seconds,
                request.skill_id,
                request.request_id,
            )
            self._force_kill_and_reap(proc)

        # 5. Join reader and writer threads boundedly
        self._join_threads(stdout_thread, stderr_thread, writer_thread)
        self._close_pipes(proc)

        raw_stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace").strip()

        # Check for watchdog timeout
        if timed_out:
            return create_error_response(
                request.request_id,
                "worker_timeout",
                f"Skill worker execution timed out after {request.timeout_seconds:.1f}s.",
            )

        # Check for stdout overflow
        if stdout_overflow.is_set() or stdout_bytes_collected > MAX_MESSAGE_BYTES:
            logger.error(
                "Worker exceeded stdout message size limit (%d bytes) for request '%s'.",
                MAX_MESSAGE_BYTES,
                request.request_id,
            )
            return create_error_response(
                request.request_id,
                "oversized_message",
                f"Worker response exceeded maximum allowed stdout limit of {MAX_MESSAGE_BYTES} bytes.",
            )

        exit_code = proc.returncode
        raw_stdout = b"".join(stdout_chunks)

        # 6. Validate framing: must be non-empty and have single line
        clean_stdout = raw_stdout.decode("utf-8", errors="replace").strip()
        if not clean_stdout:
            if exit_code == 0:
                return create_error_response(
                    request.request_id,
                    "empty_response",
                    "Worker process exited cleanly with code 0 without returning a response.",
                )
            diag_snippet = f": {raw_stderr[:200]}" if raw_stderr else ""
            return create_error_response(
                request.request_id,
                "worker_crash",
                f"Worker process crashed with exit code {exit_code}{diag_snippet}",
            )

        lines = [line for line in clean_stdout.splitlines() if line.strip()]
        if len(lines) > 1:
            logger.error("Multiple stdout response frames detected from worker for request '%s'.", request.request_id)
            return create_error_response(
                request.request_id,
                "protocol_framing_error",
                "Worker returned multiple response frames or unexpected trailing data.",
            )

        # 7. Decode response frame
        try:
            response = decode_response(lines[0])
        except ProtocolError as exc:
            return create_error_response(
                request.request_id,
                "malformed_response",
                f"Protocol validation error decoding worker response: {exc}",
            )
        except Exception as exc:
            return create_error_response(
                request.request_id,
                "malformed_response",
                f"Failed to parse worker response JSON: {exc}",
            )

        # 8. Verify security protocol invariants
        if response.request_id != request.request_id:
            logger.critical(
                "Security protocol violation: request ID mismatch! Expected '%s', got '%s'.",
                request.request_id,
                response.request_id,
            )
            return create_error_response(
                request.request_id,
                "security_protocol_violation",
                f"Mismatched request_id in response. Expected '{request.request_id}', got '{response.request_id}'.",
            )

        if response.protocol_version != PROTOCOL_VERSION:
            return create_error_response(
                request.request_id,
                "protocol_version_mismatch",
                f"Unsupported protocol version '{response.protocol_version}'. Expected '{PROTOCOL_VERSION}'.",
            )

        return response

    def _cleanup_execution(
        self,
        request_id: str,
        proc: subprocess.Popen | None,
        reservation: LaunchReservation,
    ) -> None:
        """Atomic cleanup releasing active process, launch reservation, and concurrency capacity."""
        if proc is not None:
            self._force_kill_and_reap(proc)

        with self._lock:
            if not reservation.completed:
                # Launcher thread is still executing inside OS CreateProcessW.
                # Mark cancelled, but DO NOT remove the reservation, request_id, or release semaphore yet.
                # The launcher thread (_run_popen) retains responsibility to kill the late-created
                # process, remove the reservation, and release capacity upon return.
                reservation.cancelled = True
                return

            self._active_processes.pop(request_id, None)
            self._in_progress_launches.pop(request_id, None)
            self._active_request_ids.discard(request_id)

            # If shutdown was initiated and all launches and processes are settled, complete shutdown
            if self._shutdown_initiated and not self._shutdown_completed:
                if not self._active_processes and not self._in_progress_launches:
                    self._shutdown_completed = True
                    self._shutdown_completed_event.set()

        self._semaphore.release()

    def _force_kill_and_reap(self, proc: subprocess.Popen) -> None:
        """Forcefully terminate and reap a child process, closing all open pipe descriptors."""
        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:
            pass

        try:
            proc.wait(timeout=self._cleanup_grace_period_seconds)
        except Exception:
            pass

        self._close_pipes(proc)

    @staticmethod
    def _close_pipes(proc: subprocess.Popen) -> None:
        """Close open stdin/stdout/stderr pipes safely."""
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe is not None and not pipe.closed:
                try:
                    pipe.close()
                except Exception:
                    pass

    @staticmethod
    def _join_threads(*threads: threading.Thread, timeout: float = 0.5) -> None:
        """Join reader threads within a strict bounded timeout."""
        for t in threads:
            if t.is_alive():
                t.join(timeout=timeout)

    def shutdown(self, timeout: float = 5.0) -> None:
        """Idempotently shut down the supervisor, terminating all active processes and launch reservations.

        If any launcher thread is blocked inside Windows CreateProcessW at timeout expiration,
        the launch reservation is retained so late-created processes are killed upon return.
        """
        deadline = time.monotonic() + timeout

        with self._lock:
            if self._shutdown_completed:
                return

            is_owner = not self._shutdown_initiated
            if is_owner:
                self._shutdown_initiated = True
                # Cancel all pending launch reservations immediately
                for res in self._in_progress_launches.values():
                    res.cancelled = True

        if is_owner:
            try:
                # 1. Signal kill to all active processes
                with self._lock:
                    active_procs = list(self._active_processes.values())

                for proc in active_procs:
                    try:
                        proc.kill()
                    except Exception:
                        pass

                # 2. Wait boundedly for each process to terminate and close pipes
                for proc in active_procs:
                    rem = max(0.0, deadline - time.monotonic())
                    try:
                        proc.wait(timeout=min(self._cleanup_grace_period_seconds, rem))
                    except Exception:
                        pass
                    self._close_pipes(proc)

                # 3. Allow brief moment for pending launch threads to exit Popen
                rem = max(0.0, deadline - time.monotonic())
                if rem > 0:
                    time.sleep(min(0.1, rem))

                with self._lock:
                    unresolved = [res for res in self._in_progress_launches.values() if not res.completed]
                    if unresolved or self._active_processes:
                        logger.warning(
                            "Shutdown timeout: %d launch reservation(s) still pending OS process creation and/or %d active processes remain. "
                            "Reservations will kill processes upon completion.",
                            len(unresolved),
                            len(self._active_processes),
                        )
                        return

                    self._active_processes.clear()
                    self._active_request_ids.clear()
                    self._shutdown_completed = True
            finally:
                if self._shutdown_completed:
                    self._shutdown_completed_event.set()
        else:
            # Concurrent callers wait for shutdown completion
            rem = max(0.0, deadline - time.monotonic())
            self._shutdown_completed_event.wait(timeout=rem)
