"""Safe Win32 and native NT access control, protected DACL, and handle-relative cleanup.

Provides RAII-governed memory management for native Windows security descriptors,
per-worker protected DACL generation, and race-free filesystem cleanup that eliminates
TOCTOU vulnerabilities by operating strictly through verified open kernel handles.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import sys
from typing import Any

from app.skills.sandbox.win32_acl_types import (
    CHILD_INHERIT_ONLY_ACE,
    DACL_SECURITY_INFORMATION,
    DELETE,
    ERROR_ACCESS_DENIED,
    ERROR_ALREADY_EXISTS,
    ERROR_FILE_NOT_FOUND,
    ERROR_NO_MORE_FILES,
    ERROR_SHARING_VIOLATION,
    ERROR_SUCCESS,
    EXPLICIT_ACCESS_W,
    FILE_ATTRIBUTE_DIRECTORY,
    FILE_ATTRIBUTE_NORMAL,
    FILE_ATTRIBUTE_READONLY,
    FILE_ATTRIBUTE_REPARSE_POINT,
    FILE_BASIC_INFO,
    FILE_DISPOSITION_INFO,
    FILE_FLAG_BACKUP_SEMANTICS,
    FILE_FLAG_OPEN_REPARSE_POINT,
    FILE_GENERIC_READ,
    FILE_ID_BOTH_DIR_INFO,
    FILE_OPEN,
    FILE_READ_ATTRIBUTES,
    FILE_SHARE_DELETE,
    FILE_SHARE_READ,
    FILE_SHARE_WRITE,
    FILE_SYNCHRONOUS_IO_NONALERT,
    FILE_WRITE_ATTRIBUTES,
    FileAttributeTagInfo,
    FileBasicInfo,
    FileDispositionInfo,
    FileIdBothDirectoryInfo,
    GENERIC_READ,
    IO_STATUS_BLOCK,
    NO_INHERITANCE,
    NT_FILE_OPEN_REPARSE_POINT,
    OBJ_CASE_INSENSITIVE,
    OBJECT_ATTRIBUTES,
    OPEN_EXISTING,
    PROTECTED_DACL_SECURITY_INFORMATION,
    RUNTIME_ACCESS_MASK,
    SCRATCH_CHILD_ACCESS_MASK,
    SCRATCH_ROOT_ACCESS_MASK,
    SE_DACL_DEFAULTED,
    SE_DACL_PRESENT,
    SE_DACL_PROTECTED,
    SE_FILE_OBJECT,
    SECURITY_ATTRIBUTES,
    SECURITY_DESCRIPTOR,
    SET_ACCESS,
    STATUS_ACCESS_DENIED,
    STATUS_NO_MORE_FILES,
    STATUS_SHARING_VIOLATION,
    STATUS_SUCCESS,
    SYNCHRONIZE,
    TokenPrivileges,
    TRAVERSE_ACCESS_MASK,
    TRUSTEE_IS_SID,
    TRUSTEE_IS_USER,
    TRUSTEE_W,
    UNICODE_STRING,
    advapi32,
    is_invalid_handle,
    kernel32,
    ntdll,
)


class AclError(Exception):
    """Base exception for all Windows access control and permission errors."""


class SecurityDescriptorError(AclError):
    """Raised when a security descriptor is absent, malformed, or cannot be configured."""


class SecurityPolicyConflictError(AclError):
    """Raised when an explicit access denial conflicts with required sandbox permissions."""


class NativeApiError(AclError):
    """Raised when a native NT system call fails or returns an unexpected status."""


class FilesystemCleanupError(AclError):
    """Raised when handle-relative filesystem cleanup cannot be performed safely."""


# ---------------------------------------------------------------------------
# SID and Trustee Utilities
# ---------------------------------------------------------------------------

def convert_sid_to_string(p_sid: int | ctypes.c_void_p) -> str:
    """Convert a binary PSID pointer to its SDDL string representation (e.g. S-1-15-2-...).

    The allocated native string buffer is freed using kernel32.LocalFree before returning.
    """
    if sys.platform != "win32" or advapi32 is None or kernel32 is None:
        raise AclError("convert_sid_to_string requires Windows.")

    if not p_sid:
        raise ValueError("Cannot convert NULL PSID pointer to string.")

    ptr_val = ctypes.c_void_p(p_sid.value if isinstance(p_sid, ctypes.c_void_p) else p_sid)
    str_ptr = wintypes.LPWSTR()

    ok = advapi32.ConvertSidToStringSidW(ptr_val, ctypes.byref(str_ptr))
    if not ok:
        err = ctypes.get_last_error()
        raise AclError(f"ConvertSidToStringSidW failed with Win32 error {err}.")

    try:
        return str(str_ptr.value)
    finally:
        kernel32.LocalFree(str_ptr)


def convert_string_to_sid(sid_str: str) -> int:
    """Convert an SDDL string representation into an allocated binary PSID.

    Caller assumes ownership of the returned PSID pointer and must free it via kernel32.LocalFree.
    """
    if sys.platform != "win32" or advapi32 is None:
        raise AclError("convert_string_to_sid requires Windows.")

    if not isinstance(sid_str, str) or not sid_str.strip().startswith("S-"):
        raise ValueError(f"Invalid SDDL string SID: '{sid_str}'.")

    p_sid = ctypes.c_void_p()
    ok = advapi32.ConvertStringSidToSidW(sid_str.strip(), ctypes.byref(p_sid))
    if not ok:
        err = ctypes.get_last_error()
        raise AclError(f"ConvertStringSidToSidW failed for '{sid_str}' with Win32 error {err}.")

    return int(p_sid.value or 0)


def create_trustee_for_sid(p_sid: int | ctypes.c_void_p) -> TRUSTEE_W:
    """Initialize a TRUSTEE_W structure referencing a binary PSID."""
    ptr_val = p_sid.value if isinstance(p_sid, ctypes.c_void_p) else p_sid
    trustee = TRUSTEE_W()
    trustee.pMultipleTrustee = None
    trustee.MultipleTrusteeOperation = 0
    trustee.TrusteeForm = TRUSTEE_IS_SID
    trustee.TrusteeType = TRUSTEE_IS_USER
    trustee.ptstrName = ctypes.c_void_p(ptr_val)
    return trustee


def build_explicit_access(
    p_sid: int | ctypes.c_void_p,
    access_permissions: int,
    inheritance_flags: int,
    access_mode: int = SET_ACCESS,
) -> EXPLICIT_ACCESS_W:
    """Construct an EXPLICIT_ACCESS_W structure with the specified access rights."""
    ea = EXPLICIT_ACCESS_W()
    ea.grfAccessPermissions = access_permissions
    ea.grfAccessMode = access_mode
    ea.grfInheritance = inheritance_flags
    ea.Trustee = create_trustee_for_sid(p_sid)
    return ea


# ---------------------------------------------------------------------------
# Protected DACL and Atomic Directory Creation
# ---------------------------------------------------------------------------

def create_protected_directory(
    path: Path | str,
    explicit_access_entries: list[EXPLICIT_ACCESS_W],
) -> None:
    """Atomically create a directory with SE_DACL_PROTECTED and the specified DACL.

    Guarantees that:
    1. The directory is created with its explicit DACL applied atomically by the kernel.
    2. SE_DACL_PROTECTED blocks all inheritance from permissive parent directories.
    3. All native memory allocated for the temporary security descriptor and ACL is freed.
    """
    if sys.platform != "win32" or advapi32 is None or kernel32 is None:
        raise AclError("create_protected_directory requires Windows.")

    target_path = Path(os.path.abspath(str(path)))
    if target_path.exists():
        raise FileExistsError(f"Directory already exists: {target_path}")

    if not explicit_access_entries:
        raise ValueError("explicit_access_entries must contain at least one access entry.")

    # 1. Merge explicit access entries into a new ACL
    count = len(explicit_access_entries)
    arr_type = EXPLICIT_ACCESS_W * count
    ea_array = arr_type(*explicit_access_entries)
    p_new_acl = ctypes.c_void_p()

    res = advapi32.SetEntriesInAclW(count, ea_array, None, ctypes.byref(p_new_acl))
    if res != ERROR_SUCCESS:
        raise AclError(f"SetEntriesInAclW failed with Win32 error {res}.")

    # 2. Allocate and initialize an absolute SECURITY_DESCRIPTOR
    sd = SECURITY_DESCRIPTOR()
    p_sd = ctypes.byref(sd)

    try:
        if not advapi32.InitializeSecurityDescriptor(p_sd, 1):  # SECURITY_DESCRIPTOR_REVISION = 1
            err = ctypes.get_last_error()
            raise SecurityDescriptorError(f"InitializeSecurityDescriptor failed with Win32 error {err}.")

        # Attach the new DACL
        if not advapi32.SetSecurityDescriptorDacl(p_sd, True, p_new_acl, False):
            err = ctypes.get_last_error()
            raise SecurityDescriptorError(f"SetSecurityDescriptorDacl failed with Win32 error {err}.")

        # Mark DACL as PROTECTED to prevent parent ACE inheritance
        if not advapi32.SetSecurityDescriptorControl(p_sd, SE_DACL_PROTECTED, SE_DACL_PROTECTED):
            err = ctypes.get_last_error()
            raise SecurityDescriptorError(f"SetSecurityDescriptorControl failed with Win32 error {err}.")

        # 3. Create the directory atomically with SECURITY_ATTRIBUTES
        sa = SECURITY_ATTRIBUTES()
        sa.nLength = ctypes.sizeof(sa)
        sa.lpSecurityDescriptor = ctypes.cast(p_sd, ctypes.c_void_p)
        sa.bInheritHandle = False

        if not kernel32.CreateDirectoryW(str(target_path), ctypes.byref(sa)):
            err = ctypes.get_last_error()
            raise AclError(f"CreateDirectoryW failed for '{target_path}' with Win32 error {err}.")
    finally:
        if p_new_acl.value:
            kernel32.LocalFree(p_new_acl)


def grant_path_access(
    path: Path | str,
    explicit_access_entries: list[EXPLICIT_ACCESS_W],
    protect_dacl: bool = False,
    replace_dacl: bool = False,
) -> None:
    """Safely apply explicit ACEs to an existing directory.

    If protect_dacl is True, applies PROTECTED_DACL_SECURITY_INFORMATION to set SE_DACL_PROTECTED.
    If replace_dacl is True, applies the explicit entries as a fresh DACL without merging existing parent ACEs.
    """
    if sys.platform != "win32" or advapi32 is None or kernel32 is None:
        raise AclError("grant_path_access requires Windows.")

    target_path = Path(os.path.abspath(str(path)))
    if not target_path.exists():
        raise FileNotFoundError(f"Target path does not exist: {target_path}")

    p_old_dacl = ctypes.c_void_p()
    p_sec_desc = ctypes.c_void_p()

    # Read existing DACL if merging
    if not replace_dacl:
        res = advapi32.GetNamedSecurityInfoW(
            str(target_path),
            SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION,
            None,
            None,
            ctypes.byref(p_old_dacl),
            None,
            ctypes.byref(p_sec_desc),
        )
        if res != ERROR_SUCCESS:
            raise AclError(f"GetNamedSecurityInfoW failed for '{target_path}' with Win32 error {res}.")

    p_new_acl = ctypes.c_void_p()
    count = len(explicit_access_entries)
    arr_type = EXPLICIT_ACCESS_W * count
    ea_array = arr_type(*explicit_access_entries)

    try:
        base_acl = None if replace_dacl else p_old_dacl
        res = advapi32.SetEntriesInAclW(count, ea_array, base_acl, ctypes.byref(p_new_acl))
        if res != ERROR_SUCCESS:
            raise AclError(f"SetEntriesInAclW failed for '{target_path}' with Win32 error {res}.")

        sec_info = DACL_SECURITY_INFORMATION
        if protect_dacl:
            sec_info |= PROTECTED_DACL_SECURITY_INFORMATION

        res = advapi32.SetNamedSecurityInfoW(
            str(target_path),
            SE_FILE_OBJECT,
            sec_info,
            None,
            None,
            p_new_acl,
            None,
        )
        if res != ERROR_SUCCESS:
            raise AclError(f"SetNamedSecurityInfoW failed for '{target_path}' with Win32 error {res}.")
    finally:
        if p_sec_desc.value:
            kernel32.LocalFree(p_sec_desc)
        if p_new_acl.value:
            kernel32.LocalFree(p_new_acl)


# ---------------------------------------------------------------------------
# Preflight Traversal Verification
# ---------------------------------------------------------------------------

def check_traverse_preflight() -> bool:
    """Verify whether the current process token possesses SeChangeNotifyPrivilege enabled.

    In Windows NT, SeChangeNotifyPrivilege allows bypassing intermediate directory traversal
    checks. If disabled by enterprise Group Policy, traversal checks fail closed.
    """
    if sys.platform != "win32" or advapi32 is None or kernel32 is None:
        return False

    h_token = wintypes.HANDLE()
    GetCurrentProcess = kernel32.GetCurrentProcess
    GetCurrentProcess.restype = wintypes.HANDLE

    if not advapi32.OpenProcessToken(GetCurrentProcess(), 0x0008, ctypes.byref(h_token)):  # TOKEN_QUERY
        return False

    try:
        # SeChangeNotifyPrivilege is a well-known privilege (LUID 23 / 0x17)
        # Check TokenPrivileges (enum 3)
        size = wintypes.DWORD()
        advapi32.GetTokenInformation(h_token, TokenPrivileges, None, 0, ctypes.byref(size))
        buf = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(h_token, TokenPrivileges, buf, size.value, ctypes.byref(size)):
            return False

        # Read count at offset 0
        count = ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD)).contents.value
        # Each LUID_AND_ATTRIBUTES is 12 bytes on x64 (8 byte LUID + 4 byte Attributes)
        offset = 4  # DWORD count, then array starts
        for i in range(count):
            entry_addr = ctypes.addressof(buf) + offset + (i * 12)
            low_part = ctypes.cast(entry_addr, ctypes.POINTER(wintypes.DWORD)).contents.value
            high_part = ctypes.cast(entry_addr + 4, ctypes.POINTER(wintypes.LONG)).contents.value
            attrs = ctypes.cast(entry_addr + 8, ctypes.POINTER(wintypes.DWORD)).contents.value
            if low_part == 23 and high_part == 0:  # SE_CHANGE_NOTIFY_PRIVILEGE
                return bool(attrs & 0x00000002)  # SE_PRIVILEGE_ENABLED
        return False
    finally:
        kernel32.CloseHandle(h_token)


# ---------------------------------------------------------------------------
# Handle-Relative Zero-TOCTOU Filesystem Cleanup
# ---------------------------------------------------------------------------

def validate_relative_entry_name(name: str) -> None:
    """Validate that an enumerated directory entry is strictly a single, safe relative name."""
    if not isinstance(name, str) or not name:
        raise FilesystemCleanupError("Directory entry name must be a non-empty string.")
    if "/" in name or "\\" in name:
        raise FilesystemCleanupError(f"Malicious path separator detected in entry name: '{name}'.")
    if ":" in name:
        raise FilesystemCleanupError(f"Malicious alternate data stream colon detected: '{name}'.")
    if name in (".", ".."):
        raise FilesystemCleanupError(f"Illegal relative navigation component: '{name}'.")


def open_relative_child_handle(
    h_parent: int | wintypes.HANDLE,
    child_name: str,
    desired_access: int = DELETE | SYNCHRONIZE | FILE_READ_ATTRIBUTES | FILE_WRITE_ATTRIBUTES | GENERIC_READ,
) -> int:
    """Open a child filesystem object strictly relative to an already-open parent directory handle.

    Uses native NT API ntdll.NtCreateFile with OBJECT_ATTRIBUTES.RootDirectory set to h_parent
    and FILE_OPEN_REPARSE_POINT. Path lookup is pinned directly to the parent's kernel File
    Control Block, making it impossible to traverse or be redirected outside h_parent.
    """
    if sys.platform != "win32" or ntdll is None:
        raise NativeApiError("open_relative_child_handle requires native Windows NT API.")

    validate_relative_entry_name(child_name)
    parent_handle = h_parent.value if isinstance(h_parent, wintypes.HANDLE) else h_parent

    us = UNICODE_STRING()
    us.Length = len(child_name) * 2
    us.MaximumLength = us.Length + 2
    us.Buffer = child_name

    oa = OBJECT_ATTRIBUTES()
    oa.Length = ctypes.sizeof(oa)
    oa.RootDirectory = wintypes.HANDLE(parent_handle)
    oa.ObjectName = ctypes.pointer(us)
    oa.Attributes = OBJ_CASE_INSENSITIVE
    oa.SecurityDescriptor = None
    oa.SecurityQualityOfService = None

    iosb = IO_STATUS_BLOCK()
    h_child = wintypes.HANDLE()

    status = ntdll.NtCreateFile(
        ctypes.byref(h_child),
        desired_access,
        ctypes.byref(oa),
        ctypes.byref(iosb),
        None,
        0,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        FILE_OPEN,
        NT_FILE_OPEN_REPARSE_POINT | FILE_SYNCHRONOUS_IO_NONALERT,
        None,
        0,
    )

    if status != STATUS_SUCCESS:
        raise NativeApiError(f"NtCreateFile failed for relative child '{child_name}' with status {hex(status & 0xFFFFFFFF)}.")

    return int(h_child.value or 0)


def delete_file_by_handle(h_file: int | wintypes.HANDLE) -> bool:
    """Mark an open file, junction, or directory for deletion on handle close and close it immediately.

    If deletion initially fails with ERROR_ACCESS_DENIED, clears FILE_ATTRIBUTE_READONLY
    via FileBasicInfo on the handle and retries.
    Guarantees handle is closed exactly once in all execution paths.
    """
    if sys.platform != "win32" or kernel32 is None:
        raise NativeApiError("delete_file_by_handle requires Windows.")

    handle_val = h_file.value if isinstance(h_file, wintypes.HANDLE) else h_file
    disp = FILE_DISPOSITION_INFO(DeleteFile=True)

    try:
        ok = kernel32.SetFileInformationByHandle(
            wintypes.HANDLE(handle_val),
            FileDispositionInfo,
            ctypes.byref(disp),
            ctypes.sizeof(disp),
        )
        if not ok:
            err = ctypes.get_last_error()
            if err == ERROR_ACCESS_DENIED:
                # Inspect attributes via FileBasicInfo
                fbi = FILE_BASIC_INFO()
                if kernel32.GetFileInformationByHandleEx(
                    wintypes.HANDLE(handle_val),
                    FileBasicInfo,
                    ctypes.byref(fbi),
                    ctypes.sizeof(fbi),
                ):
                    if fbi.FileAttributes & FILE_ATTRIBUTE_READONLY:
                        # Clear readonly attribute on open handle
                        fbi_set = FILE_BASIC_INFO()
                        fbi_set.FileAttributes = (fbi.FileAttributes & ~FILE_ATTRIBUTE_READONLY) or FILE_ATTRIBUTE_NORMAL
                        if kernel32.SetFileInformationByHandle(
                            wintypes.HANDLE(handle_val),
                            FileBasicInfo,
                            ctypes.byref(fbi_set),
                            ctypes.sizeof(fbi_set),
                        ):
                            # Retry disposition after clearing readonly bit
                            ok = kernel32.SetFileInformationByHandle(
                                wintypes.HANDLE(handle_val),
                                FileDispositionInfo,
                                ctypes.byref(disp),
                                ctypes.sizeof(disp),
                            )
                            if ok:
                                err = ERROR_SUCCESS
                            else:
                                err = ctypes.get_last_error()
            if not ok:
                raise FilesystemCleanupError(
                    f"SetFileInformationByHandle(FileDispositionInfo) failed with Win32 error {err}."
                )
        return True
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(handle_val))


def clean_directory_handle_relative(h_dir: int | wintypes.HANDLE) -> None:
    """Recursively enumerate and delete all children strictly through open handles.

    Invariants Enforced:
    1. Directory entries are streamed directly from h_dir using GetFileInformationByHandleEx.
    2. Zero reconstructed path strings are generated or passed to the OS.
    3. Every child handle is opened relative to h_dir via NtCreateFile with FILE_OPEN_REPARSE_POINT.
    4. Reparse points (junctions and symlinks) are unlinked without recursing into external targets.
    5. Normal subdirectories are recursed depth-first, emptied, and unlinked by handle.
    """
    if sys.platform != "win32" or kernel32 is None:
        raise FilesystemCleanupError("clean_directory_handle_relative requires Windows.")

    dir_handle = h_dir.value if isinstance(h_dir, wintypes.HANDLE) else h_dir

    # 64 KiB buffer for directory streaming
    buf_size = 65536
    buf = ctypes.create_string_buffer(buf_size)

    while True:
        ok = kernel32.GetFileInformationByHandleEx(
            wintypes.HANDLE(dir_handle),
            FileIdBothDirectoryInfo,
            buf,
            buf_size,
        )
        if not ok:
            err = ctypes.get_last_error()
            if err == ERROR_NO_MORE_FILES or err == ERROR_SUCCESS:
                break
            raise FilesystemCleanupError(f"GetFileInformationByHandleEx failed during enumeration with Win32 error {err}.")

        offset = 0
        while True:
            entry = ctypes.cast(
                ctypes.addressof(buf) + offset,
                ctypes.POINTER(FILE_ID_BOTH_DIR_INFO),
            ).contents

            name_len = entry.FileNameLength // 2
            name_addr = ctypes.addressof(buf) + offset + FILE_ID_BOTH_DIR_INFO.FileName.offset
            entry_name = ctypes.wstring_at(name_addr, name_len)

            if entry_name not in (".", ".."):
                validate_relative_entry_name(entry_name)

                # Open child relative to parent directory handle
                h_child = open_relative_child_handle(dir_handle, entry_name)
                is_reparse = bool(entry.FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT)
                is_dir = bool(entry.FileAttributes & FILE_ATTRIBUTE_DIRECTORY)
                child_closed = False

                try:
                    if is_reparse:
                        child_closed = True
                        delete_file_by_handle(h_child)
                    elif is_dir:
                        # Recurse depth-first into subdirectory handle
                        clean_directory_handle_relative(h_child)
                        # Once emptied, delete subdirectory handle
                        child_closed = True
                        delete_file_by_handle(h_child)
                    else:
                        # Delete regular file handle
                        child_closed = True
                        delete_file_by_handle(h_child)
                finally:
                    if not child_closed:
                        kernel32.CloseHandle(wintypes.HANDLE(h_child))

            if entry.NextEntryOffset == 0:
                break
            offset += entry.NextEntryOffset


def self_test_native_nt_api() -> bool:
    """Execute an automated startup verification of NtCreateFile handle-relative open.

    Fails closed if the native system call does not behave as expected.
    """
    if sys.platform != "win32" or ntdll is None or kernel32 is None:
        return False

    import tempfile
    td = tempfile.mkdtemp(prefix="anaya_nt_test_")
    try:
        test_file = os.path.join(td, "test.txt")
        with open(test_file, "w") as f:
            f.write("test")

        h_parent = kernel32.CreateFileW(
            td,
            GENERIC_READ | SYNCHRONIZE,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS,
            None,
        )
        if is_invalid_handle(h_parent):
            return False

        try:
            h_child = open_relative_child_handle(h_parent, "test.txt")
            delete_file_by_handle(h_child)
            return not os.path.exists(test_file)
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(h_parent))
    except Exception:
        return False
    finally:
        import shutil
        shutil.rmtree(td, ignore_errors=True)
