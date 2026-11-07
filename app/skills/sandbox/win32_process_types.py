"""Win32 API structures, constants, and function prototypes for extended process launching.

Provides 64-bit ctypes structures and kernel32/advapi32 entry points for:
- STARTUPINFOEXW and attribute list manipulation.
- AppContainer security capabilities and capability lists.
- Process token query and inspection (TokenIsAppContainer, TokenAppContainerSid, TokenCapabilities).
- Strict stdio pipe handle control and atomic Job Object creation-time attachment.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import sys

# Process Creation Flags
CREATE_SUSPENDED: int = 0x00000004
EXTENDED_STARTUPINFO_PRESENT: int = 0x00080000
CREATE_UNICODE_ENVIRONMENT: int = 0x00000400

# Startup Info Flags
STARTF_USESTDHANDLES: int = 0x00000100

# ProcThreadAttribute IDs (Windows 10 Version 1703+ / Server 2016+)
# Formula: ProcThreadAttributeValue(Number, Thread, Input, AddStore)
# Number 2:  PROC_THREAD_ATTRIBUTE_HANDLE_LIST          = 0x00020002
# Number 9:  PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009
# Number 13: PROC_THREAD_ATTRIBUTE_JOB_LIST             = 0x0002000D
PROC_THREAD_ATTRIBUTE_HANDLE_LIST: int = 0x00020002
PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES: int = 0x00020009
PROC_THREAD_ATTRIBUTE_JOB_LIST: int = 0x0002000D

# Token Information Classes
TokenUser: int = 1
TokenGroups: int = 2
TokenPrivileges: int = 3
TokenIsAppContainer: int = 29
TokenCapabilities: int = 30
TokenAppContainerSid: int = 31

# Token Access Masks
TOKEN_QUERY: int = 0x0008

# Process Access Masks
PROCESS_TERMINATE: int = 0x0001
PROCESS_QUERY_INFORMATION: int = 0x0400
PROCESS_QUERY_LIMITED_INFORMATION: int = 0x1000
SYNCHRONIZE: int = 0x00100000

# Wait Constants
WAIT_OBJECT_0: int = 0x00000000
WAIT_TIMEOUT: int = 0x00000102
WAIT_FAILED: int = 0xFFFFFFFF
INFINITE: int = 0xFFFFFFFF

# Process Exit Status
STILL_ACTIVE: int = 259

# Handle Flags
HANDLE_FLAG_INHERIT: int = 0x00000001
HANDLE_FLAG_PROTECT_FROM_CLOSE: int = 0x00000002

# Standard Win32 Error Codes
ERROR_SUCCESS: int = 0
ERROR_FILE_NOT_FOUND: int = 2
ERROR_ACCESS_DENIED: int = 5
ERROR_INVALID_PARAMETER: int = 87
ERROR_INSUFFICIENT_BUFFER: int = 122


class STARTUPINFOW(ctypes.Structure):
    """STARTUPINFOW structure (104 bytes on x64)."""

    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class STARTUPINFOEXW(ctypes.Structure):
    """STARTUPINFOEXW structure (112 bytes on x64)."""

    _fields_ = [
        ("StartupInfo", STARTUPINFOW),
        ("lpAttributeList", wintypes.LPVOID),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    """PROCESS_INFORMATION structure (24 bytes on x64)."""

    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class SID_AND_ATTRIBUTES(ctypes.Structure):
    """SID_AND_ATTRIBUTES structure (16 bytes on x64)."""

    _fields_ = [
        ("Sid", wintypes.LPVOID),
        ("Attributes", wintypes.DWORD),
    ]


class SECURITY_CAPABILITIES(ctypes.Structure):
    """SECURITY_CAPABILITIES structure for AppContainer configuration (24 bytes on x64)."""

    _fields_ = [
        ("AppContainerSid", wintypes.LPVOID),
        ("Capabilities", ctypes.POINTER(SID_AND_ATTRIBUTES)),
        ("CapabilityCount", wintypes.DWORD),
        ("Reserved", wintypes.DWORD),
    ]


class TOKEN_APPCONTAINER_INFORMATION(ctypes.Structure):
    """TOKEN_APPCONTAINER_INFORMATION structure (8 bytes on x64)."""

    _fields_ = [
        ("TokenAppContainer", wintypes.LPVOID),
    ]


class TOKEN_GROUPS(ctypes.Structure):
    """TOKEN_GROUPS variable-length structure."""

    _fields_ = [
        ("GroupCount", wintypes.DWORD),
        ("Groups", SID_AND_ATTRIBUTES * 1),
    ]


# Function prototypes
if sys.platform == "win32":
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    # InitializeProcThreadAttributeList
    InitializeProcThreadAttributeList = kernel32.InitializeProcThreadAttributeList
    InitializeProcThreadAttributeList.argtypes = [
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    InitializeProcThreadAttributeList.restype = wintypes.BOOL

    # UpdateProcThreadAttribute
    UpdateProcThreadAttribute = kernel32.UpdateProcThreadAttribute
    UpdateProcThreadAttribute.argtypes = [
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.c_size_t,
        wintypes.LPVOID,
        ctypes.c_size_t,
        wintypes.LPVOID,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    UpdateProcThreadAttribute.restype = wintypes.BOOL

    # DeleteProcThreadAttributeList
    DeleteProcThreadAttributeList = kernel32.DeleteProcThreadAttributeList
    DeleteProcThreadAttributeList.argtypes = [wintypes.LPVOID]
    DeleteProcThreadAttributeList.restype = None

    # CreateProcessW
    CreateProcessW = kernel32.CreateProcessW
    CreateProcessW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.BOOL,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPCWSTR,
        ctypes.POINTER(STARTUPINFOEXW),
        ctypes.POINTER(PROCESS_INFORMATION),
    ]
    CreateProcessW.restype = wintypes.BOOL

    # ResumeThread
    ResumeThread = kernel32.ResumeThread
    ResumeThread.argtypes = [wintypes.HANDLE]
    ResumeThread.restype = wintypes.DWORD

    # TerminateProcess
    TerminateProcess = kernel32.TerminateProcess
    TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    TerminateProcess.restype = wintypes.BOOL

    # WaitForSingleObject
    WaitForSingleObject = kernel32.WaitForSingleObject
    WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    WaitForSingleObject.restype = wintypes.DWORD

    # GetExitCodeProcess
    GetExitCodeProcess = kernel32.GetExitCodeProcess
    GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    GetExitCodeProcess.restype = wintypes.BOOL

    # IsProcessInJob
    IsProcessInJob = kernel32.IsProcessInJob
    IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    IsProcessInJob.restype = wintypes.BOOL

    # CreatePipe
    CreatePipe = kernel32.CreatePipe
    CreatePipe.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    CreatePipe.restype = wintypes.BOOL

    # SetHandleInformation
    SetHandleInformation = kernel32.SetHandleInformation
    SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
    SetHandleInformation.restype = wintypes.BOOL

    # CloseHandle
    CloseHandle = kernel32.CloseHandle
    CloseHandle.argtypes = [wintypes.HANDLE]
    CloseHandle.restype = wintypes.BOOL

    # OpenProcessToken
    OpenProcessToken = advapi32.OpenProcessToken
    OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    OpenProcessToken.restype = wintypes.BOOL

    # GetTokenInformation
    GetTokenInformation = advapi32.GetTokenInformation
    GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    GetTokenInformation.restype = wintypes.BOOL

    # EqualSid
    EqualSid = advapi32.EqualSid
    EqualSid.argtypes = [wintypes.LPVOID, wintypes.LPVOID]
    EqualSid.restype = wintypes.BOOL
else:
    kernel32 = None
    advapi32 = None
    InitializeProcThreadAttributeList = None
    UpdateProcThreadAttribute = None
    DeleteProcThreadAttributeList = None
    CreateProcessW = None
    ResumeThread = None
    TerminateProcess = None
    WaitForSingleObject = None
    GetExitCodeProcess = None
    IsProcessInJob = None
    CreatePipe = None
    SetHandleInformation = None
    CloseHandle = None
    OpenProcessToken = None
    GetTokenInformation = None
    EqualSid = None
