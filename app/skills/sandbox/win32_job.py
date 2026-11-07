"""Windows Job Object abstraction for process quota and lifecycle containment.

Enforces kernel-level constraints on skill worker subprocesses:
- ActiveProcessLimit = 1 (prevents subprocess spawning / fork bombs)
- Private committed virtual memory ceilings (ProcessMemoryLimit / JobMemoryLimit)
- Configurable per-process user-mode CPU time quotas (PerProcessUserTimeLimit)
- Deterministic process tree teardown (KILL_ON_JOB_CLOSE)

Security Invariant:
Job Objects provide compute and process lifecycle containment only.
They do NOT provide filesystem access control, token sandboxing, or network isolation.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import math
import sys
import threading
from typing import TYPE_CHECKING

from app.skills.sandbox.win32_types import (
    HANDLE_FLAG_INHERIT,
    JOB_OBJECT_LIMIT_ACTIVE_PROCESS,
    JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION,
    JOB_OBJECT_LIMIT_JOB_MEMORY,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    JOB_OBJECT_LIMIT_PROCESS_MEMORY,
    JOB_OBJECT_LIMIT_PROCESS_TIME,
    JOBOBJECT_BASIC_ACCOUNTING_INFORMATION,
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
    JobObjectBasicAccountingInformation,
    JobObjectExtendedLimitInformation,
)

if TYPE_CHECKING:
    import ctypes

logger = logging.getLogger("anaya.skills.sandbox.job")


class JobObjectError(Exception):
    """Base exception for all Windows Job Object operations."""


class JobObjectCreationError(JobObjectError):
    """Raised when creating a Windows Job Object fails."""


class JobObjectLimitError(JobObjectError):
    """Raised when setting or querying Job Object limits fails."""


class JobAssignmentError(JobObjectError):
    """Raised when assigning a process to a Job Object fails."""


class JobObjectQueryError(JobObjectError):
    """Raised when querying Job Object accounting information fails."""


class JobTerminationError(JobObjectError):
    """Raised when terminating a Job Object fails."""


class JobCloseError(JobObjectError):
    """Raised when closing a Job Object native handle fails."""


# Native 64-bit bounds
MAX_DWORD: int = 0xFFFFFFFF
MAX_SIZE_T: int = 0xFFFFFFFFFFFFFFFF
# PerProcessUserTimeLimit is signed 64-bit 100ns units: max seconds ~ 922,337,203,685s
MAX_CPU_SECONDS: float = 0x7FFFFFFFFFFFFFFF / 10_000_000.0


@dataclass(frozen=True)
class JobLimits:
    """Resource limits and flags for Windows Job Object configuration.

    Attributes:
        kill_on_close: If True, all processes in the job are immediately terminated
            when the last handle to the Job Object is closed.
        active_process_limit: Maximum concurrent processes in the job. Setting 1
            strictly prohibits the child process from spawning any subprocesses.
        process_memory_limit_bytes: Hard ceiling on private committed virtual memory
            pages for each process (in bytes). The Windows Virtual Memory Manager
            enforces this commit limit on virtual allocations (e.g. VirtualAlloc).
            Observable behavior upon exhaustion depends on the allocation path and
            runtime: standard Python heap allocations may raise MemoryError if
            handled by the runtime allocator, whereas unhandled allocations, low-level
            runtime initialization, or native code may result in immediate process
            termination.
        job_memory_limit_bytes: Optional hard ceiling on aggregate private committed
            virtual memory across all processes in the job, enforced by the OS.
        cpu_time_limit_seconds: Optional ceiling on per-process user-mode CPU execution
            time. If exceeded, the OS terminates the process with STATUS_QUOTA_EXCEEDED.
        die_on_unhandled_exception: If True, causes processes in the job to terminate
            immediately on unhandled exceptions without invoking the unhandled exception
            filter or attaching a debugger. Note: This is an OS exception policy flag,
            not a general application crash-dialog suppression tool.
    """

    kill_on_close: bool = True
    active_process_limit: int | None = 1
    process_memory_limit_bytes: int | None = 256 * 1024 * 1024  # 256 MiB commit cap
    job_memory_limit_bytes: int | None = None
    cpu_time_limit_seconds: float | None = None
    die_on_unhandled_exception: bool = False


@dataclass(frozen=True)
class JobAccountingInfo:
    """Snapshot of execution metrics reported by the Windows kernel for a Job Object.

    Attributes:
        active_processes: Number of processes currently running in the job.
        total_processes: Total processes assigned to the job over its lifetime.
        total_terminated_processes: Total processes terminated in the job.
        total_user_time_seconds: Accumulated user-mode CPU time across all processes.
        total_kernel_time_seconds: Accumulated kernel-mode CPU time across all processes.
        total_page_fault_count: Total page faults encountered.
    """

    active_processes: int
    total_processes: int
    total_terminated_processes: int
    total_user_time_seconds: float
    total_kernel_time_seconds: float
    total_page_fault_count: int


class Win32JobObject:
    """Manages the creation, configuration, process assignment, and lifecycle of a Windows Job Object.

    Guarantees:
        - Job handle is explicitly created non-inheritable.
        - Limits are configured prior to worker process assignment.
        - Process assignment must succeed before thread resumption.
        - close() is strictly idempotent and safe across multiple threads.
        - All native handles have single, explicit ownership.
    """

    def __init__(
        self,
        limits: JobLimits | None = None,
        name: str | None = None,
    ) -> None:
        if sys.platform != "win32":
            raise JobObjectCreationError(
                f"Windows Job Objects are not supported on platform '{sys.platform}'."
            )

        import ctypes
        from app.skills.sandbox.win32_types import (
            CloseHandle,
            CreateJobObjectW,
            SetHandleInformation,
        )

        self._lock = threading.RLock()
        self._handle: int | None = None

        # 1. Create native Job Object (anonymous by default)
        h_job = CreateJobObjectW(None, name)
        if not h_job:
            err = ctypes.get_last_error()
            raise JobObjectCreationError(
                f"CreateJobObjectW failed with Win32 error {err}: {ctypes.WinError(err)}"
            )

        self._handle = int(h_job)

        # 2. Guarantee handle is non-inheritable so child processes cannot defeat kill-on-close
        if not SetHandleInformation(h_job, HANDLE_FLAG_INHERIT, 0):
            err = ctypes.get_last_error()
            self.close()
            raise JobObjectCreationError(
                f"SetHandleInformation failed to clear HANDLE_FLAG_INHERIT: {ctypes.WinError(err)}"
            )

        # 3. Configure initial limits if provided
        if limits is not None:
            try:
                self.set_limits(limits)
            except Exception:
                self.close()
                raise

    @property
    def handle(self) -> int | None:
        """Return the raw native Win32 HANDLE, or None if closed."""
        with self._lock:
            return self._handle

    @property
    def is_closed(self) -> bool:
        """Return True if the native handle has been closed."""
        with self._lock:
            return self._handle is None

    def set_limits(self, limits: JobLimits) -> None:
        """Apply resource quotas and lifecycle limits to the Job Object."""
        with self._lock:
            if self._handle is None:
                raise JobObjectError("Cannot set limits on a closed Job Object.")

            import ctypes
            from app.skills.sandbox.win32_types import (
                SetInformationJobObject,
            )

            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            limit_flags = 0

            if type(limits.kill_on_close) is not bool:
                raise TypeError("kill_on_close must be a bool.")
            if limits.kill_on_close:
                limit_flags |= JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE

            if limits.active_process_limit is not None:
                if type(limits.active_process_limit) is bool or not isinstance(limits.active_process_limit, int):
                    raise TypeError("active_process_limit must be an integer, not bool.")
                if not (1 <= limits.active_process_limit <= MAX_DWORD):
                    raise ValueError(f"active_process_limit must be between 1 and {MAX_DWORD}.")
                limit_flags |= JOB_OBJECT_LIMIT_ACTIVE_PROCESS
                info.BasicLimitInformation.ActiveProcessLimit = limits.active_process_limit

            if limits.process_memory_limit_bytes is not None:
                if type(limits.process_memory_limit_bytes) is bool or not isinstance(limits.process_memory_limit_bytes, int):
                    raise TypeError("process_memory_limit_bytes must be an integer, not bool.")
                if not (1 <= limits.process_memory_limit_bytes <= MAX_SIZE_T):
                    raise ValueError(f"process_memory_limit_bytes must be between 1 and {MAX_SIZE_T}.")
                limit_flags |= JOB_OBJECT_LIMIT_PROCESS_MEMORY
                info.ProcessMemoryLimit = limits.process_memory_limit_bytes

            if limits.job_memory_limit_bytes is not None:
                if type(limits.job_memory_limit_bytes) is bool or not isinstance(limits.job_memory_limit_bytes, int):
                    raise TypeError("job_memory_limit_bytes must be an integer, not bool.")
                if not (1 <= limits.job_memory_limit_bytes <= MAX_SIZE_T):
                    raise ValueError(f"job_memory_limit_bytes must be between 1 and {MAX_SIZE_T}.")
                limit_flags |= JOB_OBJECT_LIMIT_JOB_MEMORY
                info.JobMemoryLimit = limits.job_memory_limit_bytes

            if limits.cpu_time_limit_seconds is not None:
                if type(limits.cpu_time_limit_seconds) is bool or not isinstance(limits.cpu_time_limit_seconds, (int, float)):
                    raise TypeError("cpu_time_limit_seconds must be a real number, not bool.")
                if (
                    not math.isfinite(limits.cpu_time_limit_seconds)
                    or limits.cpu_time_limit_seconds <= 0.0
                    or limits.cpu_time_limit_seconds > MAX_CPU_SECONDS
                ):
                    raise ValueError(
                        f"cpu_time_limit_seconds must be a positive finite number <= {MAX_CPU_SECONDS}."
                    )
                limit_flags |= JOB_OBJECT_LIMIT_PROCESS_TIME
                # Convert seconds to 100-nanosecond intervals (1s = 10,000,000 * 100ns)
                info.BasicLimitInformation.PerProcessUserTimeLimit = int(
                    limits.cpu_time_limit_seconds * 10_000_000
                )

            if type(limits.die_on_unhandled_exception) is not bool:
                raise TypeError("die_on_unhandled_exception must be a bool.")
            if limits.die_on_unhandled_exception:
                limit_flags |= JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION

            info.BasicLimitInformation.LimitFlags = limit_flags

            success = SetInformationJobObject(
                self._handle,
                JobObjectExtendedLimitInformation,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            if not success:
                err = ctypes.get_last_error()
                raise JobObjectLimitError(
                    f"SetInformationJobObject failed with error {err}: {ctypes.WinError(err)}"
                )

    def assign_process(self, process_handle: int) -> None:
        """Assign an active or suspended process to the Job Object.

        Args:
            process_handle: Raw OS process handle with PROCESS_SET_QUOTA and
                PROCESS_TERMINATE access rights.

        Raises:
            JobObjectError: If the Job Object is closed.
            JobAssignmentError: If the OS rejects the assignment (e.g. invalid handle,
                or nested job restrictions in an uncooperative host environment).
        """
        with self._lock:
            if self._handle is None:
                raise JobObjectError("Cannot assign process to a closed Job Object.")

            import ctypes
            from app.skills.sandbox.win32_types import AssignProcessToJobObject

            success = AssignProcessToJobObject(self._handle, process_handle)
            if not success:
                err = ctypes.get_last_error()
                raise JobAssignmentError(
                    f"AssignProcessToJobObject failed for process handle {process_handle} "
                    f"with error {err}: {ctypes.WinError(err)}"
                )

    def query_accounting(self) -> JobAccountingInfo:
        """Retrieve kernel process counts and CPU time metrics for the Job Object."""
        with self._lock:
            if self._handle is None:
                raise JobObjectError("Cannot query accounting on a closed Job Object.")

            import ctypes
            from ctypes import wintypes
            from app.skills.sandbox.win32_types import (
                QueryInformationJobObject,
            )

            acct = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
            return_len = wintypes.DWORD()

            success = QueryInformationJobObject(
                self._handle,
                JobObjectBasicAccountingInformation,
                ctypes.byref(acct),
                ctypes.sizeof(acct),
                ctypes.byref(return_len),
            )
            if not success:
                err = ctypes.get_last_error()
                raise JobObjectQueryError(
                    f"QueryInformationJobObject failed with error {err}: {ctypes.WinError(err)}"
                )

            return JobAccountingInfo(
                active_processes=acct.ActiveProcesses,
                total_processes=acct.TotalProcesses,
                total_terminated_processes=acct.TotalTerminatedProcesses,
                total_user_time_seconds=acct.TotalUserTime / 10_000_000.0,
                total_kernel_time_seconds=acct.TotalKernelTime / 10_000_000.0,
                total_page_fault_count=acct.TotalPageFaultCount,
            )

    def terminate(self, exit_code: int = 1) -> None:
        """Terminate all processes currently attached to the Job Object.

        Args:
            exit_code: Exit status code (0 <= exit_code <= 0xFFFFFFFF).

        Raises:
            JobObjectError: If the Job Object is closed.
            TypeError: If exit_code is not an integer or is a bool.
            ValueError: If exit_code is outside 32-bit unsigned integer range.
            JobTerminationError: If TerminateJobObject returns FALSE.
        """
        with self._lock:
            if self._handle is None:
                raise JobObjectError("Cannot terminate processes in a closed Job Object.")

            if type(exit_code) is bool or not isinstance(exit_code, int):
                raise TypeError("exit_code must be an integer, not bool.")
            if not (0 <= exit_code <= MAX_DWORD):
                raise ValueError(f"exit_code must be between 0 and {MAX_DWORD}.")

            import ctypes
            from app.skills.sandbox.win32_types import TerminateJobObject

            success = TerminateJobObject(self._handle, exit_code)
            if not success:
                err = ctypes.get_last_error()
                raise JobTerminationError(
                    f"TerminateJobObject failed with error {err}: {ctypes.WinError(err)}"
                )

    def close(self) -> None:
        """Close the native Job Object handle idempotently.

        If kill_on_close was enabled, closing the last open handle triggers
        immediate kernel-level termination of all assigned processes.

        Raises:
            JobCloseError: If CloseHandle returns FALSE.
        """
        with self._lock:
            if self._handle is not None:
                h = self._handle
                self._handle = None
                import ctypes
                from app.skills.sandbox.win32_types import CloseHandle

                success = CloseHandle(h)
                if not success:
                    err = ctypes.get_last_error()
                    raise JobCloseError(
                        f"CloseHandle failed for handle {h} with error {err}: {ctypes.WinError(err)}"
                    )

    def __enter__(self) -> "Win32JobObject":
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
