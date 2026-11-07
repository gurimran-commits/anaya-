"""Tests for Windows Job Object subsystem (Phase 10 Step 10.3.1).

Validates:
- 64-bit ctypes structure layouts, sizes, and field offsets.
- API failure and exception handling.
- Handle ownership, closure idempotency, and context management.
- Live Windows active process limit enforcement (blocking subprocess creation).
- Live Windows private commit memory limit enforcement.
- Live Windows CPU time limit quota enforcement.
- Live Windows kill-on-job-close deterministic teardown.
- Live Windows assignment failure handling.
- Handle leak prevention.
- Environment-aware nested Job Object compatibility.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from unittest import mock
import pytest

from app.skills.sandbox.win32_job import (
    JobAccountingInfo,
    JobAssignmentError,
    JobCloseError,
    JobLimits,
    JobObjectCreationError,
    JobObjectError,
    JobObjectLimitError,
    JobObjectQueryError,
    JobTerminationError,
    Win32JobObject,
)
from app.skills.sandbox.win32_types import (
    IO_COUNTERS,
    JOBOBJECT_BASIC_ACCOUNTING_INFORMATION,
    JOBOBJECT_BASIC_LIMIT_INFORMATION,
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
)

IS_WINDOWS = sys.platform == "win32"
pytestmark = pytest.mark.skipif(not IS_WINDOWS, reason="Windows Job Objects require Windows.")

if IS_WINDOWS:
    from app.skills.sandbox.win32_types import (
        CloseHandle,
        GetExitCodeProcess,
        ResumeThread,
        WaitForSingleObject,
    )


# -----------------------------------------------------------------------------
# 1. Structure Sizing & Field Offset Tests
# -----------------------------------------------------------------------------

def test_structure_sizes_and_alignments() -> None:
    """Verify that all ctypes structures match native 64-bit Windows memory layouts."""
    # IO_COUNTERS: 6 uint64 fields = 48 bytes
    assert ctypes.sizeof(IO_COUNTERS) == 48

    # JOBOBJECT_BASIC_LIMIT_INFORMATION: 64 bytes on x64
    assert ctypes.sizeof(JOBOBJECT_BASIC_LIMIT_INFORMATION) == 64

    # JOBOBJECT_EXTENDED_LIMIT_INFORMATION:
    # 64 (Basic) + 48 (IO_COUNTERS) + 4 * 8 (Memory limits) = 144 bytes on x64
    assert ctypes.sizeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION) == 144

    # JOBOBJECT_BASIC_ACCOUNTING_INFORMATION:
    # 4 int64 (32) + 4 DWORD (16) = 48 bytes on x64
    assert ctypes.sizeof(JOBOBJECT_BASIC_ACCOUNTING_INFORMATION) == 48


def test_structure_field_offsets() -> None:
    """Verify exact 64-bit field offsets to prevent struct packing or alignment bugs."""
    # IO_COUNTERS
    assert IO_COUNTERS.ReadOperationCount.offset == 0
    assert IO_COUNTERS.WriteOperationCount.offset == 8
    assert IO_COUNTERS.OtherOperationCount.offset == 16
    assert IO_COUNTERS.ReadTransferCount.offset == 24
    assert IO_COUNTERS.WriteTransferCount.offset == 32
    assert IO_COUNTERS.OtherTransferCount.offset == 40

    # JOBOBJECT_BASIC_LIMIT_INFORMATION
    assert JOBOBJECT_BASIC_LIMIT_INFORMATION.PerProcessUserTimeLimit.offset == 0
    assert JOBOBJECT_BASIC_LIMIT_INFORMATION.PerJobUserTimeLimit.offset == 8
    assert JOBOBJECT_BASIC_LIMIT_INFORMATION.LimitFlags.offset == 16
    # 4 bytes padding between LimitFlags (DWORD) and MinimumWorkingSetSize (SIZE_T)
    assert JOBOBJECT_BASIC_LIMIT_INFORMATION.MinimumWorkingSetSize.offset == 24
    assert JOBOBJECT_BASIC_LIMIT_INFORMATION.MaximumWorkingSetSize.offset == 32
    assert JOBOBJECT_BASIC_LIMIT_INFORMATION.ActiveProcessLimit.offset == 40
    # 4 bytes padding between ActiveProcessLimit (DWORD) and Affinity (ULONG_PTR)
    assert JOBOBJECT_BASIC_LIMIT_INFORMATION.Affinity.offset == 48
    assert JOBOBJECT_BASIC_LIMIT_INFORMATION.PriorityClass.offset == 56
    assert JOBOBJECT_BASIC_LIMIT_INFORMATION.SchedulingClass.offset == 60

    # JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    assert JOBOBJECT_EXTENDED_LIMIT_INFORMATION.BasicLimitInformation.offset == 0
    assert JOBOBJECT_EXTENDED_LIMIT_INFORMATION.IoInfo.offset == 64
    assert JOBOBJECT_EXTENDED_LIMIT_INFORMATION.ProcessMemoryLimit.offset == 112
    assert JOBOBJECT_EXTENDED_LIMIT_INFORMATION.JobMemoryLimit.offset == 120
    assert JOBOBJECT_EXTENDED_LIMIT_INFORMATION.PeakProcessMemoryLimit.offset == 128
    assert JOBOBJECT_EXTENDED_LIMIT_INFORMATION.PeakJobMemoryLimit.offset == 136

    # JOBOBJECT_BASIC_ACCOUNTING_INFORMATION
    assert JOBOBJECT_BASIC_ACCOUNTING_INFORMATION.TotalUserTime.offset == 0
    assert JOBOBJECT_BASIC_ACCOUNTING_INFORMATION.TotalKernelTime.offset == 8
    assert JOBOBJECT_BASIC_ACCOUNTING_INFORMATION.ThisPeriodTotalUserTime.offset == 16
    assert JOBOBJECT_BASIC_ACCOUNTING_INFORMATION.ThisPeriodTotalKernelTime.offset == 24
    assert JOBOBJECT_BASIC_ACCOUNTING_INFORMATION.TotalPageFaultCount.offset == 32
    assert JOBOBJECT_BASIC_ACCOUNTING_INFORMATION.TotalProcesses.offset == 36
    assert JOBOBJECT_BASIC_ACCOUNTING_INFORMATION.ActiveProcesses.offset == 40
    assert JOBOBJECT_BASIC_ACCOUNTING_INFORMATION.TotalTerminatedProcesses.offset == 44


# -----------------------------------------------------------------------------
# 2. Lifecycle, Context Manager & Idempotent Close Tests
# -----------------------------------------------------------------------------

def test_job_object_lifecycle_and_idempotency() -> None:
    """Verify that creation, querying, and closing work correctly and idempotently."""
    job = Win32JobObject()
    assert not job.is_closed
    assert job.handle is not None and job.handle != 0

    # Query initial accounting: 0 processes
    acct = job.query_accounting()
    assert acct.active_processes == 0
    assert acct.total_processes == 0

    # Idempotent close
    job.close()
    assert job.is_closed
    assert job.handle is None

    # Repeated close is a no-op
    job.close()
    assert job.is_closed


def test_job_object_context_manager() -> None:
    """Verify context-manager closes the native handle on exit."""
    with Win32JobObject() as job:
        assert not job.is_closed
        h = job.handle
        assert h is not None

    assert job.is_closed
    assert job.handle is None


def test_operations_on_closed_job_fail() -> None:
    """Verify operations on a closed Job Object raise JobObjectError."""
    job = Win32JobObject()
    job.close()

    with pytest.raises(JobObjectError, match="closed"):
        job.set_limits(JobLimits())

    with pytest.raises(JobObjectError, match="closed"):
        job.assign_process(1234)

    with pytest.raises(JobObjectError, match="closed"):
        job.query_accounting()


# -----------------------------------------------------------------------------
# 3. Input Validation & Error Handling Tests
# -----------------------------------------------------------------------------

def test_invalid_limit_values_and_types() -> None:
    """Verify type checking, non-finite rejection, and range bounds for JobLimits."""
    with Win32JobObject() as job:
        # 1. Reject bool where integers are required
        with pytest.raises(TypeError, match="active_process_limit must be an integer, not bool"):
            job.set_limits(JobLimits(active_process_limit=True))  # type: ignore[arg-type]

        with pytest.raises(TypeError, match="process_memory_limit_bytes must be an integer, not bool"):
            job.set_limits(JobLimits(process_memory_limit_bytes=True))  # type: ignore[arg-type]

        with pytest.raises(TypeError, match="job_memory_limit_bytes must be an integer, not bool"):
            job.set_limits(JobLimits(job_memory_limit_bytes=True))  # type: ignore[arg-type]

        with pytest.raises(TypeError, match="cpu_time_limit_seconds must be a real number, not bool"):
            job.set_limits(JobLimits(cpu_time_limit_seconds=True))  # type: ignore[arg-type]

        with pytest.raises(TypeError, match="kill_on_close must be a bool"):
            job.set_limits(JobLimits(kill_on_close="yes"))  # type: ignore[arg-type]

        with pytest.raises(TypeError, match="die_on_unhandled_exception must be a bool"):
            job.set_limits(JobLimits(die_on_unhandled_exception=1))  # type: ignore[arg-type]

        # 2. Reject non-finite and negative/zero CPU values
        import math
        for invalid_cpu in (float("nan"), float("inf"), float("-inf"), 0.0, -1.5, 1e15):
            with pytest.raises(ValueError, match="cpu_time_limit_seconds must be a positive finite number"):
                job.set_limits(JobLimits(cpu_time_limit_seconds=invalid_cpu))

        # 3. Reject values outside native field ranges (DWORD: 32-bit, SIZE_T: 64-bit)
        with pytest.raises(ValueError, match="active_process_limit must be between 1 and"):
            job.set_limits(JobLimits(active_process_limit=0))

        with pytest.raises(ValueError, match="active_process_limit must be between 1 and"):
            job.set_limits(JobLimits(active_process_limit=0x100000000))

        with pytest.raises(ValueError, match="process_memory_limit_bytes must be between 1 and"):
            job.set_limits(JobLimits(process_memory_limit_bytes=0))

        with pytest.raises(ValueError, match="process_memory_limit_bytes must be between 1 and"):
            job.set_limits(JobLimits(process_memory_limit_bytes=0x10000000000000000))

        with pytest.raises(ValueError, match="job_memory_limit_bytes must be between 1 and"):
            job.set_limits(JobLimits(job_memory_limit_bytes=-1))

        with pytest.raises(ValueError, match="job_memory_limit_bytes must be between 1 and"):
            job.set_limits(JobLimits(job_memory_limit_bytes=0x10000000000000000))

        # 4. Valid native boundary values succeed
        job.set_limits(
            JobLimits(
                active_process_limit=0xFFFFFFFF,
                process_memory_limit_bytes=0xFFFFFFFFFFFFFFFF,
                job_memory_limit_bytes=0xFFFFFFFFFFFFFFFF,
                cpu_time_limit_seconds=3600.0,
            )
        )


def test_creation_failure_on_non_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify non-Windows platforms fail closed with JobObjectCreationError."""
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(JobObjectCreationError, match="not supported on platform 'linux'"):
        Win32JobObject()


def test_creation_api_failure_handling() -> None:
    """Verify that CreateJobObjectW returning NULL raises JobObjectCreationError."""
    with mock.patch("app.skills.sandbox.win32_types.CreateJobObjectW", return_value=0):
        with pytest.raises(JobObjectCreationError, match="CreateJobObjectW failed"):
            Win32JobObject()


def test_set_limits_api_failure_handling() -> None:
    """Verify that SetInformationJobObject failure raises JobObjectLimitError."""
    with Win32JobObject() as job:
        with mock.patch("app.skills.sandbox.win32_types.SetInformationJobObject", return_value=0):
            with pytest.raises(JobObjectLimitError, match="SetInformationJobObject failed"):
                job.set_limits(JobLimits())


def test_query_accounting_api_failure_handling() -> None:
    """Verify that QueryInformationJobObject failure raises JobObjectQueryError."""
    with Win32JobObject() as job:
        with mock.patch("app.skills.sandbox.win32_types.QueryInformationJobObject", return_value=0):
            with pytest.raises(JobObjectQueryError, match="QueryInformationJobObject failed"):
                job.query_accounting()


def test_terminate_api_success_and_failure() -> None:
    """Verify TerminateJobObject success, closed handling, and error raising."""
    # 1. Closed job error
    job = Win32JobObject()
    job.close()
    with pytest.raises(JobObjectError, match="closed"):
        job.terminate(1)

    # 2. Input validation
    with Win32JobObject() as live_job:
        with pytest.raises(TypeError, match="exit_code must be an integer, not bool"):
            live_job.terminate(True)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="exit_code must be between 0 and"):
            live_job.terminate(-1)
        with pytest.raises(ValueError, match="exit_code must be between 0 and"):
            live_job.terminate(0x100000000)

        # 3. Native failure raises JobTerminationError
        with mock.patch("app.skills.sandbox.win32_types.TerminateJobObject", return_value=0):
            with pytest.raises(JobTerminationError, match="TerminateJobObject failed"):
                live_job.terminate(42)

    # 4. Live success: terminates attached process with exit code
    with Win32JobObject() as live_job:
        child_code = "import time; time.sleep(60)"
        hp, ht, pid = _spawn_suspended_python(child_code)
        try:
            live_job.assign_process(hp)
            ResumeThread(ht)
            assert WaitForSingleObject(hp, 100) == 258  # STILL RUNNING

            # Terminate via Job Object with exit code 77
            live_job.terminate(exit_code=77)
            assert WaitForSingleObject(hp, 3000) == 0

            from ctypes import wintypes
            exit_code = wintypes.DWORD()
            GetExitCodeProcess(hp, ctypes.byref(exit_code))
            assert exit_code.value == 77
        finally:
            CloseHandle(ht)
            CloseHandle(hp)


def test_close_api_failure_handling() -> None:
    """Verify CloseHandle failure raises JobCloseError, sets handle None, and is idempotent."""
    job = Win32JobObject()
    assert job.handle is not None

    with mock.patch("app.skills.sandbox.win32_types.CloseHandle", return_value=0):
        with pytest.raises(JobCloseError, match="CloseHandle failed"):
            job.close()

    # Even on native failure, handle was cleared to prevent handle reuse
    assert job.is_closed
    assert job.handle is None

    # Subsequent close() is safe and idempotent (returns None without calling native CloseHandle)
    with mock.patch("app.skills.sandbox.win32_types.CloseHandle") as mock_close:
        job.close()
        mock_close.assert_not_called()


# -----------------------------------------------------------------------------
# 4. Helper for Spawning Suspended Test Worker
# -----------------------------------------------------------------------------

def _spawn_suspended_python(inline_script: str) -> tuple[int, int, int]:
    """Helper to spawn a Python subprocess in CREATE_SUSPENDED state.

    Returns:
        (process_handle, thread_handle, pid)
    """
    import _winapi

    CREATE_SUSPENDED = 0x00000004
    cmd = f'"{sys.executable}" -c "{inline_script}"'
    hp, ht, pid, _ = _winapi.CreateProcess(None, cmd, None, None, False, CREATE_SUSPENDED, None, None, None)
    return hp, ht, pid


# -----------------------------------------------------------------------------
# 5. Live Windows Integration Tests
# -----------------------------------------------------------------------------

def test_active_process_limit_blocks_subprocesses() -> None:
    """Verify that ActiveProcessLimit = 1 prevents the worker from spawning sub-processes."""
    limits = JobLimits(
        kill_on_close=True,
        active_process_limit=1,
    )
    with Win32JobObject(limits=limits) as job:
        # Child script attempts to launch a grandchild subprocess
        child_code = (
            "import subprocess, sys\n"
            "try:\n"
            "    p = subprocess.Popen([sys.executable, '-c', 'pass'])\n"
            "    sys.exit(1)\n"
            "except OSError as exc:\n"
            "    err_code = getattr(exc, 'winerror', None)\n"
            "    if err_code == 1816:\n"
            "        sys.exit(0)\n"
            "    sys.exit(2)\n"
        )
        hp, ht, pid = _spawn_suspended_python(child_code)
        try:
            job.assign_process(hp)
            acct = job.query_accounting()
            assert acct.active_processes == 1

            ResumeThread(ht)
            WaitForSingleObject(hp, 5000)

            from ctypes import wintypes
            exit_code = wintypes.DWORD()
            GetExitCodeProcess(hp, ctypes.byref(exit_code))
            assert exit_code.value == 0
        finally:
            CloseHandle(ht)
            CloseHandle(hp)


def test_process_memory_commit_limit_enforced() -> None:
    """Verify private committed virtual memory ceiling (MemoryError on allocation)."""
    # 64 MiB private commit limit
    limits = JobLimits(
        kill_on_close=True,
        process_memory_limit_bytes=64 * 1024 * 1024,
    )
    with Win32JobObject(limits=limits) as job:
        child_code = (
            "import sys\n"
            "try:\n"
            "    data = bytearray(128 * 1024 * 1024)\n"
            "    sys.exit(1)\n"
            "except MemoryError:\n"
            "    sys.exit(42)\n"
        )
        hp, ht, pid = _spawn_suspended_python(child_code)
        try:
            job.assign_process(hp)
            ResumeThread(ht)
            wait_res = WaitForSingleObject(hp, 5000)
            assert wait_res == 0, f"Expected process to terminate, got wait result: {wait_res}"

            from ctypes import wintypes
            exit_code = wintypes.DWORD()
            res = GetExitCodeProcess(hp, ctypes.byref(exit_code))
            assert res != 0, f"GetExitCodeProcess failed: {ctypes.get_last_error()}"
            assert exit_code.value != 259, "Process is still active (STILL_ACTIVE = 259)"

            # STATUS_NO_MEMORY = 0xC0000017 (3221225495)
            STATUS_NO_MEMORY = 0xC0000017
            assert exit_code.value in (42, STATUS_NO_MEMORY), (
                f"Expected exit code 42 (handled MemoryError) or 0xC0000017 (STATUS_NO_MEMORY), "
                f"got: {hex(exit_code.value)} ({exit_code.value})"
            )
        finally:
            CloseHandle(ht)
            CloseHandle(hp)


def test_cpu_time_limit_terminates_infinite_loop() -> None:
    """Verify per-process user CPU time limit terminates a CPU-bound worker."""
    # 1.0 second user CPU quota
    limits = JobLimits(
        kill_on_close=True,
        cpu_time_limit_seconds=1.0,
    )
    with Win32JobObject(limits=limits) as job:
        # Tight busy loop consuming 100% CPU
        child_code = "while True: pass"
        hp, ht, pid = _spawn_suspended_python(child_code)
        try:
            job.assign_process(hp)
            ResumeThread(ht)

            # Wait for kernel to terminate process upon exceeding 1s CPU time
            # Give up to 8s wall-clock time for 1s user CPU quota to be reached
            wait_res = WaitForSingleObject(hp, 8000)
            assert wait_res == 0, f"Expected process termination, got wait result: {wait_res}"

            from ctypes import wintypes
            exit_code = wintypes.DWORD()
            res = GetExitCodeProcess(hp, ctypes.byref(exit_code))
            assert res != 0, f"GetExitCodeProcess failed: {ctypes.get_last_error()}"
            assert exit_code.value != 259, "Process is still active"

            # STATUS_QUOTA_EXCEEDED = 0xC0000044 (3221225540)
            STATUS_QUOTA_EXCEEDED = 0xC0000044
            assert exit_code.value == STATUS_QUOTA_EXCEEDED, (
                f"Expected exit code STATUS_QUOTA_EXCEEDED (0xC0000044), "
                f"got: {hex(exit_code.value)} ({exit_code.value})"
            )

            # Verify Job Object accounting demonstrates process execution and termination
            acct = job.query_accounting()
            assert acct.total_processes >= 1
            assert acct.total_terminated_processes >= 1
            assert acct.total_user_time_seconds >= 0.8, (
                f"Expected >= 0.8s user CPU time, got {acct.total_user_time_seconds}s"
            )
        finally:
            CloseHandle(ht)
            CloseHandle(hp)


def test_kill_on_job_close_reaps_worker() -> None:
    """Verify closing the Job Object handle immediately terminates attached processes."""
    job = Win32JobObject(limits=JobLimits(kill_on_close=True))
    child_code = "import time; time.sleep(60)"
    hp, ht, pid = _spawn_suspended_python(child_code)
    try:
        job.assign_process(hp)
        ResumeThread(ht)

        # Confirm child is running (WaitForSingleObject returns WAIT_TIMEOUT = 258)
        assert WaitForSingleObject(hp, 100) == 258

        # Close the job handle: triggers kernel-level kill on close
        job.close()

        # Child should terminate immediately
        assert WaitForSingleObject(hp, 3000) == 0

        from ctypes import wintypes
        exit_code = wintypes.DWORD()
        GetExitCodeProcess(hp, ctypes.byref(exit_code))
        # Process was forcefully terminated
        assert exit_code.value != 258
    finally:
        CloseHandle(ht)
        CloseHandle(hp)


def test_assignment_failure_handling() -> None:
    """Verify assigning an invalid process handle surfaces JobAssignmentError."""
    with Win32JobObject() as job:
        # Invalid handle value (e.g. 0xFFFFFFFE)
        with pytest.raises(JobAssignmentError, match="AssignProcessToJobObject failed"):
            job.assign_process(0xFFFFFFFE)


def test_handle_leak_verification() -> None:
    """Verify creating and closing multiple Job Objects does not leak kernel handles.

    Uses GetProcessHandleCount to observe actual OS kernel process handle table metrics.
    Accounts for normal handle variation while guaranteeing that repeated allocation
    and closure does not leak handles proportionally.
    """
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_current_process = kernel32.GetCurrentProcess
    get_current_process.restype = wintypes.HANDLE
    get_process_handle_count = kernel32.GetProcessHandleCount
    get_process_handle_count.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    get_process_handle_count.restype = wintypes.BOOL

    h_proc = get_current_process()

    # Warm-up to allow dynamic runtime/thread handles to stabilize
    for _ in range(5):
        with Win32JobObject(limits=JobLimits(kill_on_close=True)) as job:
            assert job.handle is not None
        assert job.is_closed

    initial_count = wintypes.DWORD()
    assert get_process_handle_count(h_proc, ctypes.byref(initial_count))

    iterations = 100
    for _ in range(iterations):
        with Win32JobObject(limits=JobLimits(kill_on_close=True)) as job:
            assert job.handle is not None
        assert job.is_closed

    final_count = wintypes.DWORD()
    assert get_process_handle_count(h_proc, ctypes.byref(final_count))

    delta = final_count.value - initial_count.value
    # If handles leaked 1:1, delta would be ~100.
    # Allow a small tolerance (<= 5) for ambient thread/runtime fluctuations,
    # but strictly fail if handles consistently leak.
    assert delta <= 5, (
        f"Kernel handle leak detected: handle count increased by {delta} "
        f"over {iterations} Job Object cycles (initial={initial_count.value}, final={final_count.value})."
    )


def test_nested_job_object_environment_awareness() -> None:
    """Verify nested Job Object compatibility under modern Windows.

    Windows 8+ natively supports nested Job Objects. If the test runner
    is inside an environment that restricts nested jobs, verify that
    assignment either succeeds or raises JobAssignmentError cleanly.
    """
    job1 = Win32JobObject(limits=JobLimits(kill_on_close=True))
    job2 = Win32JobObject(limits=JobLimits(kill_on_close=True))

    child_code = "import time; time.sleep(5)"
    hp, ht, pid = _spawn_suspended_python(child_code)
    try:
        job1.assign_process(hp)
        assert job1.query_accounting().active_processes == 1

        # Attempt to assign to job2 (nested)
        try:
            job2.assign_process(hp)
            assert job2.query_accounting().active_processes == 1
        except JobAssignmentError as exc:
            # If the OS environment restricts nesting, it must fail with controlled error
            assert "AssignProcessToJobObject failed" in str(exc)
    finally:
        CloseHandle(ht)
        CloseHandle(hp)
        job1.close()
        job2.close()
