"""Win32 API signatures and ctypes structures for Windows Job Objects and sandboxing.

Provides verified 64-bit memory layout structures and kernel32 entry points
for process quota and lifecycle containment under Python standard library ctypes.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import sys

# Job Object Information Classes
JobObjectBasicAccountingInformation: int = 1
JobObjectBasicLimitInformation: int = 2
JobObjectExtendedLimitInformation: int = 9

# Job Object Limit Flags
# Note: Memory limits apply to private committed virtual memory pages (backed by RAM
# or pagefile), NOT resident working set (physical RAM).
JOB_OBJECT_LIMIT_WORKINGSET: int = 0x00000001
JOB_OBJECT_LIMIT_PROCESS_TIME: int = 0x00000002
JOB_OBJECT_LIMIT_JOB_TIME: int = 0x00000004
JOB_OBJECT_LIMIT_ACTIVE_PROCESS: int = 0x00000008
JOB_OBJECT_LIMIT_AFFINITY: int = 0x00000010
JOB_OBJECT_LIMIT_PRIORITY_CLASS: int = 0x00000020
JOB_OBJECT_LIMIT_PRESERVE_JOB_TIME: int = 0x00000040
JOB_OBJECT_LIMIT_SCHEDULING_CLASS: int = 0x00000080
JOB_OBJECT_LIMIT_PROCESS_MEMORY: int = 0x00000100
JOB_OBJECT_LIMIT_JOB_MEMORY: int = 0x00000200
# DIE_ON_UNHANDLED_EXCEPTION: Process terminates immediately without invoking the
# unhandled exception filter / debugger. It is NOT a general crash-dialog suppressor.
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION: int = 0x00000400
JOB_OBJECT_LIMIT_BREAKAWAY_OK: int = 0x00000800
JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK: int = 0x00001000
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: int = 0x00002000
JOB_OBJECT_LIMIT_SUBSET_AFFINITY: int = 0x00004000

# Handle flags
HANDLE_FLAG_INHERIT: int = 0x00000001
HANDLE_FLAG_PROTECT_FROM_CLOSE: int = 0x00000002


class IO_COUNTERS(ctypes.Structure):
    """IO_COUNTERS struct representing I/O accounting metrics (48 bytes on x64)."""

    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    """JOBOBJECT_BASIC_LIMIT_INFORMATION struct (64 bytes on x64)."""

    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),  # 100-nanosecond intervals
        ("PerJobUserTimeLimit", ctypes.c_int64),      # 100-nanosecond intervals
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    """JOBOBJECT_EXTENDED_LIMIT_INFORMATION struct (144 bytes on x64)."""

    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),      # Process committed memory cap in bytes
        ("JobMemoryLimit", ctypes.c_size_t),          # Job-wide committed memory cap in bytes
        ("PeakProcessMemoryLimit", ctypes.c_size_t),
        ("PeakJobMemoryLimit", ctypes.c_size_t),
    ]


class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
    """JOBOBJECT_BASIC_ACCOUNTING_INFORMATION struct (48 bytes on x64)."""

    _fields_ = [
        ("TotalUserTime", ctypes.c_int64),            # 100-nanosecond intervals
        ("TotalKernelTime", ctypes.c_int64),          # 100-nanosecond intervals
        ("ThisPeriodTotalUserTime", ctypes.c_int64),
        ("ThisPeriodTotalKernelTime", ctypes.c_int64),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


# Win32 Function Bindings
if sys.platform == "win32":
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    # HANDLE CreateJobObjectW(LPSECURITY_ATTRIBUTES lpJobAttributes, LPCWSTR lpName)
    CreateJobObjectW = kernel32.CreateJobObjectW
    CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    CreateJobObjectW.restype = wintypes.HANDLE

    # BOOL SetInformationJobObject(HANDLE hJob, JOBOBJECTINFOCLASS JobObjectInformationClass,
    #                              LPVOID lpJobObjectInfo, DWORD cbJobObjectInfoLength)
    SetInformationJobObject = kernel32.SetInformationJobObject
    SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    SetInformationJobObject.restype = wintypes.BOOL

    # BOOL QueryInformationJobObject(HANDLE hJob, JOBOBJECTINFOCLASS JobObjectInformationClass,
    #                                LPVOID lpJobObjectInfo, DWORD cbJobObjectInfoLength,
    #                                LPDWORD lpReturnLength)
    QueryInformationJobObject = kernel32.QueryInformationJobObject
    QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    QueryInformationJobObject.restype = wintypes.BOOL

    # BOOL AssignProcessToJobObject(HANDLE hJob, HANDLE hProcess)
    AssignProcessToJobObject = kernel32.AssignProcessToJobObject
    AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    AssignProcessToJobObject.restype = wintypes.BOOL

    # BOOL TerminateJobObject(HANDLE hJob, UINT uExitCode)
    TerminateJobObject = kernel32.TerminateJobObject
    TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    TerminateJobObject.restype = wintypes.BOOL

    # BOOL CloseHandle(HANDLE hObject)
    CloseHandle = kernel32.CloseHandle
    CloseHandle.argtypes = [wintypes.HANDLE]
    CloseHandle.restype = wintypes.BOOL

    # BOOL SetHandleInformation(HANDLE hObject, DWORD dwMask, DWORD dwFlags)
    SetHandleInformation = kernel32.SetHandleInformation
    SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
    SetHandleInformation.restype = wintypes.BOOL

    # BOOL ResumeThread(HANDLE hThread)
    ResumeThread = kernel32.ResumeThread
    ResumeThread.argtypes = [wintypes.HANDLE]
    ResumeThread.restype = wintypes.DWORD

    # DWORD WaitForSingleObject(HANDLE hHandle, DWORD dwMilliseconds)
    WaitForSingleObject = kernel32.WaitForSingleObject
    WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    WaitForSingleObject.restype = wintypes.DWORD

    # BOOL GetExitCodeProcess(HANDLE hProcess, LPDWORD lpExitCode)
    GetExitCodeProcess = kernel32.GetExitCodeProcess
    GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    GetExitCodeProcess.restype = wintypes.BOOL
else:
    kernel32 = None
    CreateJobObjectW = None
    SetInformationJobObject = None
    QueryInformationJobObject = None
    AssignProcessToJobObject = None
    TerminateJobObject = None
    CloseHandle = None
    SetHandleInformation = None
    ResumeThread = None
    WaitForSingleObject = None
    GetExitCodeProcess = None
