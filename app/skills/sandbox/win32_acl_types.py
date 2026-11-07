"""Win32 and NT API ctypes signatures, structures, and constants for ACLs and AppContainers.

Provides verified 64-bit structure layouts and function prototypes for:
- Windows Discretionary Access Control Lists (DACLs)
- Windows AppContainer profiles and security capabilities
- Native NT handle-relative path resolution and object deletion
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import sys

# Standard Win32 Security Object Types
SE_FILE_OBJECT: int = 1
SE_KERNEL_OBJECT: int = 6

# Security Information Flags
OWNER_SECURITY_INFORMATION: int = 0x00000001
GROUP_SECURITY_INFORMATION: int = 0x00000002
DACL_SECURITY_INFORMATION: int = 0x00000004
SACL_SECURITY_INFORMATION: int = 0x00000008
UNPROTECTED_DACL_SECURITY_INFORMATION: int = 0x40000000
PROTECTED_DACL_SECURITY_INFORMATION: int = 0x80000000

# Access Modes for EXPLICIT_ACCESS_W
NOT_USED_ACCESS: int = 0
GRANT_ACCESS: int = 1
SET_ACCESS: int = 2
DENY_ACCESS: int = 3
REVOKE_ACCESS: int = 4
SET_AUDIT_SUCCESS: int = 5
SET_AUDIT_FAILURE: int = 6

# Multiple Trustee Operations
NO_MULTIPLE_TRUSTEE: int = 0
TRUSTEE_IS_IMPERSONATE: int = 1

# Trustee Forms
TRUSTEE_IS_SID: int = 0
TRUSTEE_IS_NAME: int = 1
TRUSTEE_BAD_FORM: int = 2
TRUSTEE_IS_OBJECTS_AND_SID: int = 3
TRUSTEE_IS_OBJECTS_AND_NAME: int = 4

# Trustee Types
TRUSTEE_IS_UNKNOWN: int = 0
TRUSTEE_IS_USER: int = 1
TRUSTEE_IS_GROUP: int = 2
TRUSTEE_IS_DOMAIN: int = 3
TRUSTEE_IS_ALIAS: int = 4
TRUSTEE_IS_WELL_KNOWN_GROUP: int = 5
TRUSTEE_IS_DELETED: int = 6
TRUSTEE_IS_INVALID: int = 7
TRUSTEE_IS_COMPUTER: int = 8

# Inheritance Flags for ACEs
NO_INHERITANCE: int = 0x00
OBJECT_INHERIT_ACE: int = 0x01
CONTAINER_INHERIT_ACE: int = 0x02
NO_PROPAGATE_INHERIT_ACE: int = 0x04
INHERIT_ONLY_ACE: int = 0x08
INHERITED_ACE: int = 0x10
SUB_CONTAINERS_AND_OBJECTS_INHERIT: int = OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE
CHILD_INHERIT_ONLY_ACE: int = INHERIT_ONLY_ACE | CONTAINER_INHERIT_ACE | OBJECT_INHERIT_ACE  # 0x0B

# Security Descriptor Control Flags
SE_DACL_PRESENT: int = 0x0004
SE_DACL_DEFAULTED: int = 0x0008
SE_DACL_AUTO_INHERIT_REQ: int = 0x0100
SE_DACL_AUTO_INHERITED: int = 0x0400
SE_DACL_PROTECTED: int = 0x1000  # Prevents inheritance from parent container

# Standard and Specific Access Masks
DELETE: int = 0x00010000
READ_CONTROL: int = 0x00020000
WRITE_DAC: int = 0x00040000
WRITE_OWNER: int = 0x00080000
SYNCHRONIZE: int = 0x00100000

STANDARD_RIGHTS_REQUIRED: int = 0x000F0000
STANDARD_RIGHTS_READ: int = READ_CONTROL
STANDARD_RIGHTS_WRITE: int = READ_CONTROL
STANDARD_RIGHTS_EXECUTE: int = READ_CONTROL
STANDARD_RIGHTS_ALL: int = 0x001F0000

# File-Specific Access Rights
FILE_READ_DATA: int = 0x0001
FILE_LIST_DIRECTORY: int = 0x0001
FILE_WRITE_DATA: int = 0x0002
FILE_ADD_FILE: int = 0x0002
FILE_APPEND_DATA: int = 0x0004
FILE_ADD_SUBDIRECTORY: int = 0x0004
FILE_CREATE_PIPE_INSTANCE: int = 0x0004
FILE_READ_EA: int = 0x0008
FILE_WRITE_EA: int = 0x0010
FILE_EXECUTE: int = 0x0020
FILE_TRAVERSE: int = 0x0020
FILE_DELETE_CHILD: int = 0x0040
FILE_READ_ATTRIBUTES: int = 0x0080
FILE_WRITE_ATTRIBUTES: int = 0x0100

# Generic and Compound Rights
GENERIC_READ: int = 0x80000000
GENERIC_WRITE: int = 0x40000000
GENERIC_EXECUTE: int = 0x20000000
GENERIC_ALL: int = 0x10000000

FILE_ALL_ACCESS: int = STANDARD_RIGHTS_REQUIRED | SYNCHRONIZE | 0x01FF
FILE_GENERIC_READ: int = STANDARD_RIGHTS_READ | FILE_READ_DATA | FILE_READ_ATTRIBUTES | FILE_READ_EA | SYNCHRONIZE
FILE_GENERIC_WRITE: int = STANDARD_RIGHTS_WRITE | FILE_WRITE_DATA | FILE_WRITE_ATTRIBUTES | FILE_WRITE_EA | FILE_APPEND_DATA | SYNCHRONIZE
FILE_GENERIC_EXECUTE: int = STANDARD_RIGHTS_EXECUTE | FILE_READ_ATTRIBUTES | FILE_EXECUTE | SYNCHRONIZE

# Anaya Custom Sandbox Access Profiles
RUNTIME_ACCESS_MASK: int = FILE_GENERIC_READ | FILE_GENERIC_EXECUTE  # 0x001200A9
SCRATCH_CHILD_ACCESS_MASK: int = FILE_GENERIC_READ | FILE_GENERIC_WRITE | DELETE  # 0x0013019F
SCRATCH_ROOT_ACCESS_MASK: int = (
    FILE_LIST_DIRECTORY
    | FILE_ADD_FILE
    | FILE_ADD_SUBDIRECTORY
    | FILE_READ_EA
    | FILE_WRITE_EA
    | FILE_TRAVERSE
    | FILE_READ_ATTRIBUTES
    | FILE_WRITE_ATTRIBUTES
    | READ_CONTROL
    | SYNCHRONIZE
)  # 0x001201BF (Strictly NO DELETE, NO FILE_DELETE_CHILD, NO WRITE_DAC, NO WRITE_OWNER)
TRAVERSE_ACCESS_MASK: int = FILE_TRAVERSE | SYNCHRONIZE  # 0x00100020

# Well-Known SIDs
ALL_APPLICATION_PACKAGES_SID: str = "S-1-15-2-1"

# File System Attributes and Open Options
FILE_ATTRIBUTE_READONLY: int = 0x00000001
FILE_ATTRIBUTE_DIRECTORY: int = 0x00000010
FILE_ATTRIBUTE_NORMAL: int = 0x00000080
FILE_ATTRIBUTE_REPARSE_POINT: int = 0x00000400

FILE_FLAG_BACKUP_SEMANTICS: int = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT: int = 0x00200000
FILE_FLAG_DELETE_ON_CLOSE: int = 0x04000000

FILE_SHARE_READ: int = 0x00000001
FILE_SHARE_WRITE: int = 0x00000002
FILE_SHARE_DELETE: int = 0x00000004

CREATE_NEW: int = 1
CREATE_ALWAYS: int = 2
OPEN_EXISTING: int = 3
OPEN_ALWAYS: int = 4
TRUNCATE_EXISTING: int = 5

# Native NT Create Options
FILE_OPEN: int = 1
FILE_SYNCHRONOUS_IO_NONALERT: int = 0x00000020
NT_FILE_OPEN_REPARSE_POINT: int = 0x00200000
OBJ_CASE_INSENSITIVE: int = 0x00000040

# File Information Classes
FileBasicInfo: int = 0
FileDispositionInfo: int = 4
FileAttributeTagInfo: int = 9
FileIdBothDirectoryInfo: int = 10

# Token Information Classes
TokenUser: int = 1
TokenGroups: int = 2
TokenPrivileges: int = 3
TokenOwner: int = 4
TokenPrimaryGroup: int = 5
TokenDefaultDacl: int = 6
TokenIsAppContainer: int = 29
TokenAppContainerSid: int = 30
TokenCapabilities: int = 31
TokenAppContainerNumber: int = 32

# Process Creation Attributes
PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES: int = 0x00020009
PROC_THREAD_ATTRIBUTE_JOB_LIST: int = 0x0002000D
PROC_THREAD_ATTRIBUTE_HANDLE_LIST: int = 0x00020002
EXTENDED_STARTUPINFO_PRESENT: int = 0x00080000
CREATE_SUSPENDED: int = 0x00000004

# Common Win32 / HRESULT Error Codes
ERROR_SUCCESS: int = 0
ERROR_FILE_NOT_FOUND: int = 2
ERROR_ACCESS_DENIED: int = 5
ERROR_INVALID_HANDLE: int = 6
ERROR_NOT_ENOUGH_MEMORY: int = 8
ERROR_NO_MORE_FILES: int = 18
ERROR_SHARING_VIOLATION: int = 32
ERROR_FILE_EXISTS: int = 80
ERROR_INVALID_PARAMETER: int = 87
ERROR_INSUFFICIENT_BUFFER: int = 122
ERROR_ALREADY_EXISTS: int = 183
ERROR_INVALID_SID: int = 1337

INVALID_HANDLE_VALUE: int = 0xFFFFFFFFFFFFFFFF


def is_invalid_handle(h: Any) -> bool:
    """Return True if handle is NULL, -1, or 0xFFFFFFFFFFFFFFFF (64-bit INVALID_HANDLE_VALUE)."""
    if h is None:
        return True
    val = h.value if isinstance(h, wintypes.HANDLE) else h
    return val == 0 or val == -1 or val == 0xFFFFFFFFFFFFFFFF or val == -1

HRESULT_S_OK: int = 0
HRESULT_ALREADY_EXISTS: int = -2147024713  # 0x800700B7 (HRESULT_FROM_WIN32(ERROR_ALREADY_EXISTS))
HRESULT_FILE_NOT_FOUND: int = -2147024894  # 0x80070002 (HRESULT_FROM_WIN32(ERROR_FILE_NOT_FOUND))
HRESULT_PATH_NOT_FOUND: int = -2147024893  # 0x80070003 (HRESULT_FROM_WIN32(ERROR_PATH_NOT_FOUND))
HRESULT_ERROR_NOT_FOUND: int = -2147023728  # 0x80070490 (HRESULT_FROM_WIN32(ERROR_NOT_FOUND))
HRESULT_ACCESS_DENIED: int = -2147024891   # 0x80070005 (E_ACCESSDENIED)

# NTSTATUS Success and Warning / Error Codes
STATUS_SUCCESS: int = 0x00000000
STATUS_NO_MORE_FILES: int = -2147483642  # 0x80000006
STATUS_SHARING_VIOLATION: int = -1073741757  # 0xC0000043
STATUS_ACCESS_DENIED: int = -1073741790  # 0xC0000022


# ---------------------------------------------------------------------------
# CTypes Structure Definitions (Strict 64-bit Alignments)
# ---------------------------------------------------------------------------

class TRUSTEE_W(ctypes.Structure):
    """TRUSTEE_W struct for Win32 Access Control API (32 bytes on x64)."""

    _fields_ = [
        ("pMultipleTrustee", ctypes.c_void_p),
        ("MultipleTrusteeOperation", ctypes.c_int),
        ("TrusteeForm", ctypes.c_int),
        ("TrusteeType", ctypes.c_int),
        ("ptstrName", ctypes.c_void_p),
    ]


class EXPLICIT_ACCESS_W(ctypes.Structure):
    """EXPLICIT_ACCESS_W struct for SetEntriesInAclW (48 bytes on x64)."""

    _fields_ = [
        ("grfAccessPermissions", wintypes.DWORD),
        ("grfAccessMode", ctypes.c_int),
        ("grfInheritance", wintypes.DWORD),
        ("Trustee", TRUSTEE_W),
    ]


class ACL(ctypes.Structure):
    """ACL header struct (8 bytes on x64)."""

    _fields_ = [
        ("AclRevision", wintypes.BYTE),
        ("Sbz1", wintypes.BYTE),
        ("AclSize", wintypes.WORD),
        ("AceCount", wintypes.WORD),
        ("Sbz2", wintypes.WORD),
    ]


class SECURITY_DESCRIPTOR(ctypes.Structure):
    """SECURITY_DESCRIPTOR structure (40 bytes on x64)."""

    _fields_ = [
        ("Revision", wintypes.BYTE),
        ("Sbz1", wintypes.BYTE),
        ("Control", wintypes.WORD),
        ("Owner", ctypes.c_void_p),
        ("Group", ctypes.c_void_p),
        ("Sacl", ctypes.c_void_p),
        ("Dacl", ctypes.c_void_p),
    ]


class SECURITY_ATTRIBUTES(ctypes.Structure):
    """SECURITY_ATTRIBUTES structure (24 bytes on x64)."""

    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wintypes.BOOL),
    ]


class SID_AND_ATTRIBUTES(ctypes.Structure):
    """SID_AND_ATTRIBUTES structure (16 bytes on x64)."""

    _fields_ = [
        ("Sid", ctypes.c_void_p),
        ("Attributes", wintypes.DWORD),
    ]


class SECURITY_CAPABILITIES(ctypes.Structure):
    """SECURITY_CAPABILITIES structure for AppContainer processes (32 bytes on x64)."""

    _fields_ = [
        ("AppContainerSid", ctypes.c_void_p),
        ("Capabilities", ctypes.c_void_p),  # Pointer to SID_AND_ATTRIBUTES array
        ("CapabilityCount", wintypes.DWORD),
        ("Reserved", wintypes.DWORD),
    ]


class UNICODE_STRING(ctypes.Structure):
    """Native NT UNICODE_STRING structure (16 bytes on x64)."""

    _fields_ = [
        ("Length", wintypes.USHORT),
        ("MaximumLength", wintypes.USHORT),
        ("Buffer", wintypes.LPWSTR),
    ]


class OBJECT_ATTRIBUTES(ctypes.Structure):
    """Native NT OBJECT_ATTRIBUTES structure (48 bytes on x64)."""

    _fields_ = [
        ("Length", wintypes.ULONG),
        ("RootDirectory", wintypes.HANDLE),
        ("ObjectName", ctypes.POINTER(UNICODE_STRING)),
        ("Attributes", wintypes.ULONG),
        ("SecurityDescriptor", ctypes.c_void_p),
        ("SecurityQualityOfService", ctypes.c_void_p),
    ]


class IO_STATUS_BLOCK(ctypes.Structure):
    """Native NT IO_STATUS_BLOCK structure (16 bytes on x64)."""

    _fields_ = [
        ("Status", ctypes.c_long),
        ("Information", ctypes.c_size_t),
    ]


class FILE_BASIC_INFO(ctypes.Structure):
    """FILE_BASIC_INFO structure for Get/SetFileInformationByHandle (40 bytes on x64)."""

    _fields_ = [
        ("CreationTime", wintypes.LARGE_INTEGER),
        ("LastAccessTime", wintypes.LARGE_INTEGER),
        ("LastWriteTime", wintypes.LARGE_INTEGER),
        ("ChangeTime", wintypes.LARGE_INTEGER),
        ("FileAttributes", wintypes.DWORD),
    ]


class FILE_DISPOSITION_INFO(ctypes.Structure):
    """FILE_DISPOSITION_INFO structure for SetFileInformationByHandle (1 byte)."""

    _fields_ = [
        ("DeleteFile", wintypes.BOOLEAN),
    ]


class FILE_ATTRIBUTE_TAG_INFO(ctypes.Structure):
    """FILE_ATTRIBUTE_TAG_INFO structure for GetFileInformationByHandleEx (8 bytes)."""

    _fields_ = [
        ("FileAttributes", wintypes.DWORD),
        ("ReparseTag", wintypes.DWORD),
    ]


class FILE_ID_BOTH_DIR_INFO(ctypes.Structure):
    """FILE_ID_BOTH_DIR_INFO for directory enumeration via GetFileInformationByHandleEx."""

    _fields_ = [
        ("NextEntryOffset", wintypes.DWORD),
        ("FileIndex", wintypes.DWORD),
        ("CreationTime", wintypes.LARGE_INTEGER),
        ("LastAccessTime", wintypes.LARGE_INTEGER),
        ("LastWriteTime", wintypes.LARGE_INTEGER),
        ("ChangeTime", wintypes.LARGE_INTEGER),
        ("EndOfFile", wintypes.LARGE_INTEGER),
        ("AllocationSize", wintypes.LARGE_INTEGER),
        ("FileAttributes", wintypes.DWORD),
        ("FileNameLength", wintypes.DWORD),
        ("EaSize", wintypes.DWORD),
        ("ShortNameLength", ctypes.c_byte),
        ("ShortName", wintypes.WCHAR * 12),
        ("FileId", wintypes.LARGE_INTEGER),
        ("FileName", wintypes.WCHAR * 1),
    ]


# ---------------------------------------------------------------------------
# DLL Loader and Native Function Prototype Initialization
# ---------------------------------------------------------------------------

advapi32: ctypes.WinDLL | None = None
kernel32: ctypes.WinDLL | None = None
userenv: ctypes.WinDLL | None = None
ntdll: ctypes.WinDLL | None = None
ole32: ctypes.WinDLL | None = None

if sys.platform == "win32":
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    userenv = ctypes.WinDLL("userenv", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)

    # advapi32 Function Signatures
    advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

    advapi32.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL

    advapi32.FreeSid.argtypes = [ctypes.c_void_p]
    advapi32.FreeSid.restype = ctypes.c_void_p

    advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD

    advapi32.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD

    advapi32.SetEntriesInAclW.argtypes = [
        wintypes.ULONG,
        ctypes.POINTER(EXPLICIT_ACCESS_W),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.SetEntriesInAclW.restype = wintypes.DWORD

    advapi32.InitializeSecurityDescriptor.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    advapi32.InitializeSecurityDescriptor.restype = wintypes.BOOL

    advapi32.SetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p,
        wintypes.BOOL,
        ctypes.c_void_p,
        wintypes.BOOL,
    ]
    advapi32.SetSecurityDescriptorDacl.restype = wintypes.BOOL

    advapi32.SetSecurityDescriptorControl.argtypes = [
        ctypes.c_void_p,
        wintypes.WORD,
        wintypes.WORD,
    ]
    advapi32.SetSecurityDescriptorControl.restype = wintypes.BOOL

    advapi32.GetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    ]
    advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL

    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL

    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL

    # kernel32 Function Signatures
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    kernel32.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(SECURITY_ATTRIBUTES)]
    kernel32.CreateDirectoryW.restype = wintypes.BOOL

    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE

    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    kernel32.DeleteFileW.argtypes = [wintypes.LPCWSTR]
    kernel32.DeleteFileW.restype = wintypes.BOOL

    kernel32.MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    kernel32.MoveFileExW.restype = wintypes.BOOL

    kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    kernel32.FlushFileBuffers.restype = wintypes.BOOL

    kernel32.WriteFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    kernel32.WriteFile.restype = wintypes.BOOL

    kernel32.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL

    kernel32.SetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetFileInformationByHandle.restype = wintypes.BOOL

    kernel32.GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD

    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.LARGE_INTEGER),
        ctypes.POINTER(wintypes.LARGE_INTEGER),
        ctypes.POINTER(wintypes.LARGE_INTEGER),
        ctypes.POINTER(wintypes.LARGE_INTEGER),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL

    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE

    kernel32.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    kernel32.IsProcessInJob.restype = wintypes.BOOL

    # userenv Function Signatures
    userenv.CreateAppContainerProfile.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    userenv.CreateAppContainerProfile.restype = ctypes.c_long  # HRESULT

    userenv.DeleteAppContainerProfile.argtypes = [wintypes.LPCWSTR]
    userenv.DeleteAppContainerProfile.restype = ctypes.c_long  # HRESULT

    userenv.DeriveAppContainerSidFromAppContainerName.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    userenv.DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_long  # HRESULT

    userenv.GetAppContainerFolderPath.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    userenv.GetAppContainerFolderPath.restype = ctypes.c_long  # HRESULT

    # ole32 Function Signatures
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole32.CoTaskMemFree.restype = None

    # ntdll Native Function Signatures
    ntdll.NtCreateFile.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        ctypes.POINTER(OBJECT_ATTRIBUTES),
        ctypes.POINTER(IO_STATUS_BLOCK),
        ctypes.POINTER(wintypes.LARGE_INTEGER),
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        ctypes.c_void_p,
        wintypes.ULONG,
    ]
    ntdll.NtCreateFile.restype = ctypes.c_long  # NTSTATUS
