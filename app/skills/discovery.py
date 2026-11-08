"""Safe filesystem discovery engine and registry integration for external skill packages (Phase 10.4.2).

Discovers external Anaya skill packages from:
    ~/.anaya/skills/<package-dir>/skill.json

Security Guarantees & Pinned Boundaries:
    - Never uses importlib, exec, eval, compile, or dynamic Python source imports in the host.
    - Never reads, imports, or executes skill.py or any external Python code.
    - Windows Root-to-Manifest Chain:
        1. Verifies ancestor hierarchy from volume root down to skills root handle-relatively.
        2. Retains verified root directory handle (h_root) with reparse-point protection.
        3. Pins package directory opening relative to h_root via NtCreateFile (open_relative_child_handle).
        4. Pins manifest opening relative to h_pkg via NtCreateFile with NT_FILE_OPEN_REPARSE_POINT.
        5. Detects concurrent file modifications during read via pre- and post-read metadata verification.
    - POSIX Root-to-Manifest Chain:
        1. Verifies ancestor hierarchy from filesystem root down to skills root without following symlinks.
        2. Retains verified root directory file descriptor (fd_root).
        3. Opens package directories relative to fd_root via dir_fd with O_DIRECTORY | O_NOFOLLOW.
        4. Opens skill.json relative to fd_pkg via dir_fd with O_NOFOLLOW.
        5. Detects concurrent file modifications during read via pre- and post-read fstat checks.
    - Unsupported Platform Policy:
        Fails closed with a security rejection if safe handle-relative or no-follow capabilities
        are unavailable on the host platform.
    - Concurrency & Registry Publication:
        Scan-and-register operations are serialized per registry via registry.scan_lock and protected
        by monotonic generation checks to prevent stale snapshot overwrites. Ordinary registry reads
        remain non-blocking and atomic under registry._lock.
    - Execution Gate:
        ExternalSkillProxy completely prevents in-process execution before Phase 10.4.3.
"""

from __future__ import annotations

from dataclasses import dataclass
import errno
import logging
import os
from pathlib import Path
import re
import stat
import sys
from typing import TYPE_CHECKING, Any, Sequence

from app.skills.manifest import (
    MAX_MANIFEST_BYTES,
    ManifestValidationError,
    SkillManifest,
    parse_skill_manifest,
)
from app.skills.models import Skill, SkillMetadata, SkillResult

if TYPE_CHECKING:
    from app.skills.skill_registry import SkillRegistry

logger = logging.getLogger("anaya.skills.discovery")

# Limits & Defaults
DEFAULT_SKILLS_DIR_NAME: str = "skills"
MANIFEST_FILENAME: str = "skill.json"
MAX_PACKAGE_DIRECTORIES: int = 100

# Pattern for valid package directory name (matches valid skill ID suffix after 'user.')
PACKAGE_NAME_PATTERN: re.Pattern[str] = re.compile(r"^[a-zA-Z0-9_.-]+$")

# Win32 APIs and constants (dynamically checked on Windows)
if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes
    from app.skills.sandbox.win32_acl import NativeApiError, open_relative_child_handle
    from app.skills.sandbox.win32_acl_types import (
        ERROR_FILE_NOT_FOUND,
        ERROR_NO_MORE_FILES,
        ERROR_SUCCESS,
        FILE_ATTRIBUTE_DIRECTORY,
        FILE_ATTRIBUTE_REPARSE_POINT,
        FILE_ATTRIBUTE_TAG_INFO,
        FILE_BASIC_INFO,
        FILE_FLAG_BACKUP_SEMANTICS,
        FILE_FLAG_OPEN_REPARSE_POINT,
        FILE_GENERIC_READ,
        FILE_ID_BOTH_DIR_INFO,
        FILE_READ_ATTRIBUTES,
        FILE_SHARE_DELETE,
        FILE_SHARE_READ,
        FILE_SHARE_WRITE,
        FileAttributeTagInfo,
        FileBasicInfo,
        FileIdBothDirectoryInfo,
        OPEN_EXISTING,
        SYNCHRONIZE,
        is_invalid_handle,
    )
    from app.skills.sandbox.win32_types import kernel32

    FILE_ATTRIBUTE_DEVICE: int = 0x00000040
    FILE_LIST_DIRECTORY: int = 0x0001
else:  # pragma: no cover
    ctypes = None  # type: ignore[assignment]
    wintypes = None  # type: ignore[assignment]
    kernel32 = None  # type: ignore[assignment]
    FILE_LIST_DIRECTORY = 0x0001


def get_default_skills_root() -> Path:
    """Return the default external skills root directory (~/.anaya/skills)."""
    return Path.home() / ".anaya" / DEFAULT_SKILLS_DIR_NAME


@dataclass(frozen=True)
class RejectedPackage:
    """Details of an external package rejected during discovery."""

    package_name: str
    package_path: str
    reason: str
    error_code: str


class ExternalSkillProxy(Skill):
    """Metadata-only proxy for an external skill package discovered on disk.

    Guarantees:
        - Holds validated SkillMetadata, manifest, and package directory path.
        - Identifies as an external, isolated skill (is_external = True).
        - Does NOT import or execute package code.
        - Cannot execute through the trusted in-process path; calling execute()
          fails closed with a controlled error message.
        - Security Authority: `package_path` is recorded for informational, diagnostic,
          and logging purposes only. It is NOT an execution or staging authority.
          Phase 10.4.3 staging must independently reopen and validate every package
          relative to the verified skills root handle before staging or executing.
    """

    def __init__(
        self,
        metadata: SkillMetadata,
        package_path: Path,
        manifest: SkillManifest | None = None,
    ) -> None:
        self.metadata = metadata
        self.package_path = package_path
        self.manifest = manifest

    @property
    def is_external(self) -> bool:
        """Indicate that this skill is an external package managed in isolation."""
        return True

    def execute(self, inputs: dict[str, Any]) -> SkillResult:
        """Reject in-process execution attempt with a controlled failure."""
        return SkillResult(
            success=False,
            error="external_skill_execution_unavailable",
            message=(
                f"External skill '{self.metadata.id}' cannot be executed directly in-process. "
                "Isolated execution runtime is deferred to Phase 10.4.3."
            ),
        )


@dataclass(frozen=True)
class SkillDiscoveryResult:
    """Structured result of scanning an external skills root directory."""

    root_path: Path
    skills: tuple[ExternalSkillProxy, ...] = ()
    rejected: tuple[RejectedPackage, ...] = ()
    generation: int | None = None

    @property
    def valid_skills(self) -> tuple[ExternalSkillProxy, ...]:
        """Alias for skills tuple."""
        return self.skills

    @property
    def rejected_packages(self) -> tuple[RejectedPackage, ...]:
        """Alias for rejected tuple."""
        return self.rejected

    @property
    def manifests(self) -> tuple[SkillManifest, ...]:
        """Return tuple of manifests for all discovered skills."""
        return tuple(s.manifest for s in self.skills if s.manifest is not None)

    def get_skill(self, skill_id: str) -> ExternalSkillProxy | None:
        """Retrieve a discovered skill by ID."""
        for skill in self.skills:
            if skill.metadata.id == skill_id:
                return skill
        return None

    def get_rejection(self, package_name: str) -> RejectedPackage | None:
        """Retrieve rejection details by package directory name."""
        for rej in self.rejected:
            if rej.package_name == package_name:
                return rej
        return None


# =============================================================================
# Windows Pinned Handle Operations
# =============================================================================

def _is_dos_device_mapping(drive_str: str) -> bool:
    """Detect whether a drive specification is a virtual MS-DOS device mapping (e.g. subst drive).

    Object Manager drive substitutions reside under \\??\\ and are not reported as NTFS
    reparse points by the filesystem driver. Explicitly check QueryDosDeviceW.
    """
    if kernel32 is None or not hasattr(kernel32, "QueryDosDeviceW"):
        return False
    drive = drive_str.rstrip("\\")
    if len(drive) != 2 or drive[1] != ":":
        return False
    buf = ctypes.create_unicode_buffer(1024)
    res = kernel32.QueryDosDeviceW(drive, buf, 1024)
    if res == 0:
        return False
    target = buf.value
    return target.startswith("\\??\\")


def _open_verified_root_windows(root_path: Path) -> tuple[int | None, str | None, str | None]:
    """Verify entire ancestor hierarchy from volume root and return open root directory handle.

    Guarantees:
        - Rejects virtual MS-DOS device mappings (e.g. subst drives).
        - Starts at the volume root and steps down each ancestor handle-relatively via NtCreateFile.
        - Rejects any ancestor that is a reparse point (junction, symlink, mount point) or non-directory.
        - Grants FILE_LIST_DIRECTORY on the leaf root handle for handle-anchored directory enumeration.
        - Keeps intermediate handles closed, retaining only the final verified root handle.
        - Zero TOCTOU between ancestor verification and caller package scanning.

    Returns:
        (h_root, None, None) on success.
        (None, error_code, reason) on failure/rejection.
    """
    if kernel32 is None:  # pragma: no cover
        return None, "platform_unavailable", "Windows kernel32 is unavailable."

    target_path = Path(os.path.abspath(str(root_path)))
    parts = list(target_path.parts)
    if not parts:
        return None, "error", "Invalid root path."

    # Reject virtual DOS device mappings (e.g. subst drives)
    if _is_dos_device_mapping(parts[0]):
        return None, "device_mapping_rejected", (
            f"Volume root '{parts[0]}' is a virtual device substitution (subst drive). "
            "Virtual drive mappings are not permitted for external skills roots."
        )

    desired_access_leaf = FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES | SYNCHRONIZE
    desired_access_ancestor = FILE_READ_ATTRIBUTES | SYNCHRONIZE

    # 1. Open volume root with reparse point protection
    h_curr = kernel32.CreateFileW(
        parts[0],
        desired_access_leaf if len(parts) == 1 else desired_access_ancestor,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None,
        OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if is_invalid_handle(h_curr):
        err = ctypes.get_last_error()
        if err in (ERROR_FILE_NOT_FOUND, 3):
            return None, "root_not_found", f"Volume root '{parts[0]}' not found."
        return None, "root_access_error", f"Failed to access volume root '{parts[0]}': WinError {err}."

    fati = FILE_ATTRIBUTE_TAG_INFO()
    if not kernel32.GetFileInformationByHandleEx(
        wintypes.HANDLE(h_curr),
        FileAttributeTagInfo,
        ctypes.byref(fati),
        ctypes.sizeof(fati),
    ):
        kernel32.CloseHandle(wintypes.HANDLE(h_curr))
        return None, "root_access_error", "Failed to retrieve volume root attributes."

    if fati.FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
        kernel32.CloseHandle(wintypes.HANDLE(h_curr))
        return None, "reparse_point_detected", f"Volume root '{parts[0]}' is a reparse point."

    if not (fati.FileAttributes & FILE_ATTRIBUTE_DIRECTORY):
        kernel32.CloseHandle(wintypes.HANDLE(h_curr))
        return None, "not_a_directory", f"Volume root '{parts[0]}' is not a directory."

    # 2. Step down every intermediate ancestor and the leaf handle-relatively
    for idx, part in enumerate(parts[1:], 1):
        is_leaf = (idx == len(parts) - 1)
        access = desired_access_leaf if is_leaf else desired_access_ancestor
        try:
            h_next = open_relative_child_handle(
                h_curr,
                part,
                desired_access=access,
            )
        except NativeApiError as exc:
            kernel32.CloseHandle(wintypes.HANDLE(h_curr))
            err_str = str(exc).lower()
            if "0xc0000034" in err_str or "0xc000003a" in err_str:
                return None, "root_not_found", f"Path component '{part}' not found."
            return None, "root_access_error", f"Failed to open path component '{part}': {exc}."
        except Exception as exc:
            kernel32.CloseHandle(wintypes.HANDLE(h_curr))
            return None, "root_access_error", f"Unexpected error opening path component '{part}': {exc}."

        kernel32.CloseHandle(wintypes.HANDLE(h_curr))

        fati_next = FILE_ATTRIBUTE_TAG_INFO()
        if not kernel32.GetFileInformationByHandleEx(
            wintypes.HANDLE(h_next),
            FileAttributeTagInfo,
            ctypes.byref(fati_next),
            ctypes.sizeof(fati_next),
        ):
            kernel32.CloseHandle(wintypes.HANDLE(h_next))
            return None, "root_access_error", f"Failed to retrieve attributes for '{part}'."

        if fati_next.FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
            kernel32.CloseHandle(wintypes.HANDLE(h_next))
            return None, "reparse_point_detected", f"Path component '{part}' is a Windows reparse point (junction or symlink)."

        if not (fati_next.FileAttributes & FILE_ATTRIBUTE_DIRECTORY):
            kernel32.CloseHandle(wintypes.HANDLE(h_next))
            return None, "not_a_directory", f"Path component '{part}' is not a directory."

        h_curr = h_next

    return h_curr, None, None


def _enumerate_directory_entries_windows(
    h_root: int,
) -> tuple[list[tuple[str, bool, bool]] | None, str | None, str | None]:
    """Enumerate child entries directly from the verified h_root directory handle.

    Guarantees:
        - Uses native GetFileInformationByHandleEx with FileIdBothDirectoryInfo.
        - Directory stream is anchored directly to the verified h_root kernel handle.
        - Zero absolute-path re-traversal or string-path lookups.
        - Validates every record offset, alignment, and filename bounds strictly.
        - Rejects malformed native directory records with a controlled scan_error.

    Returns:
        (entries, None, None) on success, where each entry is (name, is_dir, is_reparse).
        (None, error_code, error_reason) on failure.
    """
    if kernel32 is None:  # pragma: no cover
        return None, "platform_unavailable", "Windows kernel32 is unavailable."

    buf_size = 65536
    buf = ctypes.create_string_buffer(buf_size)
    entries: list[tuple[str, bool, bool]] = []
    min_record_header = FILE_ID_BOTH_DIR_INFO.FileName.offset

    while True:
        ok = kernel32.GetFileInformationByHandleEx(
            wintypes.HANDLE(h_root),
            FileIdBothDirectoryInfo,
            buf,
            buf_size,
        )
        if not ok:
            err = ctypes.get_last_error()
            if err in (ERROR_NO_MORE_FILES, ERROR_SUCCESS):
                break
            return None, "scan_error", f"Directory enumeration failed via handle: WinError {err}."

        offset = 0
        while True:
            # 1. Validate that the record header fits within buffer boundaries
            if offset + min_record_header > buf_size:
                return None, "scan_error", (
                    f"Malformed directory record: record header at offset {offset} exceeds buffer boundary."
                )

            entry = ctypes.cast(
                ctypes.addressof(buf) + offset,
                ctypes.POINTER(FILE_ID_BOTH_DIR_INFO),
            ).contents

            # 2. Validate FileNameLength: must be positive, even, and fit entirely in buffer
            name_len_bytes = entry.FileNameLength
            if name_len_bytes <= 0 or (name_len_bytes % 2) != 0:
                return None, "scan_error", (
                    f"Malformed directory record: invalid FileNameLength {name_len_bytes}."
                )

            if offset + min_record_header + name_len_bytes > buf_size:
                return None, "scan_error", (
                    f"Malformed directory record: filename bytes ({name_len_bytes} B) exceed buffer boundary."
                )

            # 3. If there is a next record, validate filename fits within current record allocation
            next_offset = entry.NextEntryOffset
            if next_offset != 0 and (min_record_header + name_len_bytes > next_offset):
                return None, "scan_error", (
                    f"Malformed directory record: filename bytes exceed NextEntryOffset boundary {next_offset}."
                )

            # 4. Decode filename safely
            name_len_wchars = name_len_bytes // 2
            name_addr = ctypes.addressof(buf) + offset + min_record_header
            entry_name = ctypes.wstring_at(name_addr, name_len_wchars)

            if entry_name not in (".", ".."):
                is_dir = bool(entry.FileAttributes & FILE_ATTRIBUTE_DIRECTORY)
                is_reparse = bool(entry.FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT)
                entries.append((entry_name, is_dir, is_reparse))

            # 5. Final record in this buffer batch
            if next_offset == 0:
                break

            # 6. Validate NextEntryOffset alignment, minimum size, and buffer limits
            if next_offset < (min_record_header + name_len_bytes):
                return None, "scan_error", (
                    f"Malformed directory record: NextEntryOffset {next_offset} smaller than minimum record size."
                )

            if (next_offset % 8) != 0:
                return None, "scan_error", (
                    f"Malformed directory record: NextEntryOffset {next_offset} is not 8-byte aligned."
                )

            if offset + next_offset >= buf_size:
                return None, "scan_error", (
                    f"Malformed directory record: NextEntryOffset {next_offset} exceeds buffer boundary."
                )

            offset += next_offset

    return entries, None, None


def _read_manifest_bytes_windows(h_root: int, pkg_name: str) -> tuple[bytes | None, str | None, str | None]:
    """Open and read skill.json strictly handle-relative to open h_root handle on Windows.

    Guarantees:
        - h_pkg is opened strictly relative to h_root via NtCreateFile with NT_FILE_OPEN_REPARSE_POINT.
        - Verifies h_pkg is an ordinary directory and not a reparse point.
        - h_file is opened strictly relative to h_pkg via NtCreateFile with NT_FILE_OPEN_REPARSE_POINT.
        - Inspects h_file attributes (rejects reparse points, directories, devices).
        - Queries FILE_BASIC_INFO before read (records LastWriteTime and ChangeTime).
        - Queries GetFileSizeEx before read (records initial_size <= MAX_MANIFEST_BYTES).
        - Allocates bounded buffer (never more than MAX_MANIFEST_BYTES + 1).
        - Reads bytes directly from pinned h_file.
        - Re-checks GetFileSizeEx and FILE_BASIC_INFO post-read to detect concurrent modification during reading.
        - Cleanly closes handles in finally blocks.

    Sharing Mode Limitation:
        Windows open_relative_child_handle opens the manifest with read/write/delete sharing
        so active processes are not blocked from closing normal file streams. If a concurrent
        writer modifies the manifest in-place during the read, before/after FILE_BASIC_INFO
        checks on LastWriteTime and ChangeTime detect and reject observable mutations. However,
        if a writer is actively in-flight during the read, file read tearing could occur;
        such payloads will fail JSON schema parsing in parse_skill_manifest. Mandatory locking
        across separate processes cannot be guaranteed by Win32 user-mode without risk of
        deadlocks or sharing violation failures on open files.
    """
    if kernel32 is None:  # pragma: no cover
        return None, "platform_unavailable", "Windows kernel32 is unavailable."

    # 1. Open package directory strictly relative to pinned h_root handle
    try:
        h_pkg = open_relative_child_handle(
            h_root,
            pkg_name,
            desired_access=FILE_READ_ATTRIBUTES | SYNCHRONIZE,
        )
    except NativeApiError as exc:
        err_str = str(exc).lower()
        if "0xc0000034" in err_str or "0xc000003a" in err_str:
            return None, "directory_not_found", f"Package directory '{pkg_name}' not found."
        return None, "directory_access_error", f"Failed to open package directory '{pkg_name}': {exc}."
    except Exception as exc:
        return None, "directory_access_error", f"Failed to open package directory '{pkg_name}': {exc}."

    h_file = None
    try:
        fati_pkg = FILE_ATTRIBUTE_TAG_INFO()
        if not kernel32.GetFileInformationByHandleEx(
            wintypes.HANDLE(h_pkg),
            FileAttributeTagInfo,
            ctypes.byref(fati_pkg),
            ctypes.sizeof(fati_pkg),
        ):
            return None, "directory_access_error", f"Failed to retrieve attributes for '{pkg_name}'."

        if fati_pkg.FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
            return None, "reparse_point_detected", f"Package directory '{pkg_name}' is a Windows reparse point (junction or symlink)."

        if not (fati_pkg.FileAttributes & FILE_ATTRIBUTE_DIRECTORY):
            return None, "not_a_directory", f"Package path '{pkg_name}' is not a directory."

        # 2. Open child skill.json strictly relative to pinned h_pkg handle
        try:
            h_file = open_relative_child_handle(
                h_pkg,
                MANIFEST_FILENAME,
                desired_access=FILE_GENERIC_READ | FILE_READ_ATTRIBUTES,
            )
        except NativeApiError as exc:
            err_str = str(exc).lower()
            if "0xc0000034" in err_str or "0xc000003a" in err_str:
                return None, "missing_manifest", f"Manifest file '{MANIFEST_FILENAME}' not found in package directory."
            return None, "manifest_access_error", f"Failed to open manifest '{MANIFEST_FILENAME}': {exc}."
        except Exception as exc:
            return None, "manifest_access_error", f"Failed to open manifest '{MANIFEST_FILENAME}': {exc}."
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(h_pkg))

    # 3. Verify attributes, read bounded bytes, and detect modification during read
    try:
        fati_file = FILE_ATTRIBUTE_TAG_INFO()
        if not kernel32.GetFileInformationByHandleEx(
            wintypes.HANDLE(h_file),
            FileAttributeTagInfo,
            ctypes.byref(fati_file),
            ctypes.sizeof(fati_file),
        ):
            return None, "manifest_access_error", "Failed to retrieve manifest file information."

        if fati_file.FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
            return None, "reparse_point_detected", f"Manifest '{MANIFEST_FILENAME}' is a reparse point or symbolic link."

        if fati_file.FileAttributes & FILE_ATTRIBUTE_DIRECTORY:
            return None, "invalid_manifest", f"Manifest '{MANIFEST_FILENAME}' is a directory, not a regular file."

        if fati_file.FileAttributes & FILE_ATTRIBUTE_DEVICE:
            return None, "invalid_manifest", f"Manifest '{MANIFEST_FILENAME}' is a device, not a regular file."

        basic_before = FILE_BASIC_INFO()
        if not kernel32.GetFileInformationByHandleEx(
            wintypes.HANDLE(h_file),
            FileBasicInfo,
            ctypes.byref(basic_before),
            ctypes.sizeof(basic_before),
        ):
            return None, "manifest_access_error", "Failed to query manifest file timestamps."

        file_size_before = wintypes.LARGE_INTEGER()
        if not kernel32.GetFileSizeEx(wintypes.HANDLE(h_file), ctypes.byref(file_size_before)):
            return None, "manifest_access_error", "Failed to query manifest file size."

        initial_size = file_size_before.value
        if initial_size > MAX_MANIFEST_BYTES:
            return None, "manifest_too_large", f"Manifest size ({initial_size} bytes) exceeds maximum allowed limit of {MAX_MANIFEST_BYTES} bytes."

        # Allocate strictly bounded buffer (never more than MAX_MANIFEST_BYTES + 1)
        read_capacity = min(initial_size + 1, MAX_MANIFEST_BYTES + 1)
        buf = ctypes.create_string_buffer(read_capacity)
        bytes_read = wintypes.DWORD()
        if not kernel32.ReadFile(wintypes.HANDLE(h_file), buf, read_capacity, ctypes.byref(bytes_read), None):
            return None, "manifest_read_error", "Failed to read manifest file contents."

        actual_read = bytes_read.value
        if actual_read > MAX_MANIFEST_BYTES:
            return None, "manifest_too_large", f"Manifest payload exceeded maximum limit of {MAX_MANIFEST_BYTES} bytes."

        # Check post-read file size and timestamps
        basic_after = FILE_BASIC_INFO()
        if not kernel32.GetFileInformationByHandleEx(
            wintypes.HANDLE(h_file),
            FileBasicInfo,
            ctypes.byref(basic_after),
            ctypes.sizeof(basic_after),
        ):
            return None, "manifest_access_error", "Failed to query post-read manifest file timestamps."

        file_size_after = wintypes.LARGE_INTEGER()
        if not kernel32.GetFileSizeEx(wintypes.HANDLE(h_file), ctypes.byref(file_size_after)):
            return None, "manifest_access_error", "Failed to query post-read manifest file size."

        post_size = file_size_after.value
        if (
            post_size != initial_size
            or actual_read != initial_size
            or basic_after.LastWriteTime != basic_before.LastWriteTime
            or basic_after.ChangeTime != basic_before.ChangeTime
        ):
            return None, "manifest_modified_during_read", (
                f"Manifest file changed during reading (initial size: {initial_size}, "
                f"bytes read: {actual_read}, post size: {post_size})."
            )

        return bytes(buf.raw[:actual_read]), None, None
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(h_file))


# =============================================================================
# POSIX Pinned Descriptor Operations
# =============================================================================

def _open_verified_root_posix(root_path: Path) -> tuple[int | None, str | None, str | None]:
    """Verify entire ancestor hierarchy from filesystem root and return open root directory descriptor.

    Guarantees:
        - Fails closed immediately if platform lacks O_NOFOLLOW.
        - Steps down each ancestor directory descriptor-relatively via open(..., dir_fd=...).
        - Rejects any ancestor that is a symlink or non-directory.
        - Retains only the final verified root descriptor.
    """
    if not hasattr(os, "O_NOFOLLOW"):
        return None, "security_check_failed", "Platform does not support O_NOFOLLOW."

    target_path = Path(os.path.abspath(str(root_path)))
    parts = list(target_path.parts)
    if not parts:
        return None, "error", "Invalid root path."

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)

    try:
        fd_curr = os.open(parts[0], flags)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EEXIST):
            return None, "reparse_point_detected", f"Symlink detected in root ancestor '{parts[0]}'."
        return None, "root_access_error", f"Failed to open root component '{parts[0]}': {exc}."

    try:
        st = os.fstat(fd_curr)
        if not stat.S_ISDIR(st.st_mode):
            os.close(fd_curr)
            return None, "not_a_directory", f"Path component '{parts[0]}' is not a directory."
    except OSError as exc:
        os.close(fd_curr)
        return None, "root_access_error", f"Failed to fstat '{parts[0]}': {exc}."

    for part in parts[1:]:
        try:
            fd_next = os.open(part, flags, dir_fd=fd_curr)
        except OSError as exc:
            os.close(fd_curr)
            if exc.errno in (errno.ELOOP, errno.EEXIST):
                return None, "reparse_point_detected", f"Symlink detected in root ancestor '{part}'."
            if exc.errno == errno.ENOENT:
                return None, "root_not_found", f"Path component '{part}' not found."
            return None, "root_access_error", f"Failed to open path component '{part}': {exc}."

        os.close(fd_curr)
        fd_curr = fd_next

        try:
            st_next = os.fstat(fd_curr)
            if not stat.S_ISDIR(st_next.st_mode):
                os.close(fd_curr)
                return None, "not_a_directory", f"Path component '{part}' is not a directory."
        except OSError as exc:
            os.close(fd_curr)
            return None, "root_access_error", f"Failed to fstat '{part}': {exc}."

    return fd_curr, None, None


def _read_manifest_bytes_posix(fd_root: int, pkg_name: str) -> tuple[bytes | None, str | None, str | None]:
    """Open and read skill.json strictly descriptor-relative to open fd_root on POSIX."""
    if not hasattr(os, "O_NOFOLLOW"):
        return None, "security_check_failed", "Platform does not support O_NOFOLLOW."

    flags_dir = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        fd_pkg = os.open(pkg_name, flags_dir, dir_fd=fd_root)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EEXIST):
            return None, "reparse_point_detected", f"Package directory '{pkg_name}' is a symbolic link."
        if exc.errno == errno.ENOENT:
            return None, "directory_not_found", f"Package directory '{pkg_name}' not found."
        return None, "directory_access_error", f"Failed to access package directory '{pkg_name}': {exc}."

    flags_file = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        try:
            fd_manifest = os.open(MANIFEST_FILENAME, flags_file, dir_fd=fd_pkg)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.EEXIST):
                return None, "reparse_point_detected", f"Manifest '{MANIFEST_FILENAME}' is a symbolic link."
            if exc.errno == errno.ENOENT:
                return None, "missing_manifest", f"Manifest file '{MANIFEST_FILENAME}' not found in package directory."
            return None, "manifest_access_error", f"Failed to open manifest '{MANIFEST_FILENAME}': {exc}."
    finally:
        os.close(fd_pkg)

    try:
        st_before = os.fstat(fd_manifest)
        if not stat.S_ISREG(st_before.st_mode):
            return None, "invalid_manifest", f"Manifest '{MANIFEST_FILENAME}' is not a regular file."
        if st_before.st_size > MAX_MANIFEST_BYTES:
            return None, "manifest_too_large", f"Manifest size ({st_before.st_size} bytes) exceeds maximum limit of {MAX_MANIFEST_BYTES} bytes."

        content = os.read(fd_manifest, MAX_MANIFEST_BYTES + 1)
        if len(content) > MAX_MANIFEST_BYTES:
            return None, "manifest_too_large", f"Manifest payload exceeded maximum limit of {MAX_MANIFEST_BYTES} bytes."

        st_after = os.fstat(fd_manifest)
        mtime_ns_before = getattr(st_before, "st_mtime_ns", int(st_before.st_mtime * 1e9))
        mtime_ns_after = getattr(st_after, "st_mtime_ns", int(st_after.st_mtime * 1e9))
        ctime_ns_before = getattr(st_before, "st_ctime_ns", int(st_before.st_ctime * 1e9))
        ctime_ns_after = getattr(st_after, "st_ctime_ns", int(st_after.st_ctime * 1e9))

        if (
            st_after.st_dev != st_before.st_dev
            or st_after.st_ino != st_before.st_ino
            or st_after.st_size != len(content)
            or st_after.st_size != st_before.st_size
            or mtime_ns_after != mtime_ns_before
            or ctime_ns_after != ctime_ns_before
        ):
            return None, "manifest_modified_during_read", (
                f"Manifest file changed during reading (before: {st_before.st_size}B, "
                f"read: {len(content)}B, after: {st_after.st_size}B)."
            )

        return content, None, None
    finally:
        os.close(fd_manifest)


def read_manifest_bytes_safely(package_dir: Path) -> tuple[bytes | None, str | None, str | None]:
    """Safely open and read package_dir/skill.json using verified parent handle pinning."""
    parent_dir = package_dir.parent
    pkg_name = package_dir.name
    if sys.platform == "win32" and kernel32 is not None:
        h_root, err_code, err_reason = _open_verified_root_windows(parent_dir)
        if h_root is None:
            return None, err_code, err_reason
        try:
            return _read_manifest_bytes_windows(h_root, pkg_name)
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(h_root))
    elif hasattr(os, "O_NOFOLLOW"):
        fd_root, err_code, err_reason = _open_verified_root_posix(parent_dir)
        if fd_root is None:
            return None, err_code, err_reason
        try:
            return _read_manifest_bytes_posix(fd_root, pkg_name)
        finally:
            os.close(fd_root)
    else:
        return None, "security_check_failed", "Platform does not support safe handle-relative or no-follow directory operations."


# =============================================================================
# Discovery Engine Public Functions
# =============================================================================

def discover_external_skills(
    root_path: Path | str | None = None,
) -> SkillDiscoveryResult:
    """Discover valid external skill packages exactly one level below root_path.

    Enforces:
        - Default root: ~/.anaya/skills.
        - Verified handle/descriptor pinning from root down to leaf manifest.
        - Directory count capped at MAX_PACKAGE_DIRECTORIES (100).
        - Rejection of symlinks, junctions, reparse points, and non-regular files.
        - Detection of concurrent manifest modifications during reading.
        - Consistency between directory name and manifest ID: directory 'weather' -> 'user.weather'.
        - Duplicate ID rejection.
        - Structured reporting of valid skills and rejected packages.
    """
    if root_path is None:
        root = get_default_skills_root()
    elif isinstance(root_path, str):
        root = Path(root_path)
    else:
        root = root_path

    # If root does not exist, return empty clean discovery result
    if not root.exists():
        return SkillDiscoveryResult(root_path=root, skills=(), rejected=())

    # Fast preliminary checks on root path
    if root.is_symlink():
        return SkillDiscoveryResult(
            root_path=root,
            skills=(),
            rejected=(
                RejectedPackage(
                    package_name=root.name,
                    package_path=str(root),
                    reason="Skills root directory is a symbolic link.",
                    error_code="reparse_point_detected",
                ),
            ),
        )

    if not root.is_dir():
        return SkillDiscoveryResult(
            root_path=root,
            skills=(),
            rejected=(
                RejectedPackage(
                    package_name=root.name,
                    package_path=str(root),
                    reason="Skills root path is not a directory.",
                    error_code="not_a_directory",
                ),
            ),
        )

    # Open verified root handle / descriptor with ancestor reparse-point protection
    h_root: int | None = None
    fd_root: int | None = None

    if sys.platform == "win32" and kernel32 is not None:
        try:
            h_root, err_code, err_reason = _open_verified_root_windows(root)
            if h_root is None:
                return SkillDiscoveryResult(
                    root_path=root,
                    skills=(),
                    rejected=(
                        RejectedPackage(
                            package_name=root.name,
                            package_path=str(root),
                            reason=err_reason or "Failed to open verified root handle.",
                            error_code=err_code or "root_access_error",
                        ),
                    ),
                )
        except Exception as exc:
            return SkillDiscoveryResult(
                root_path=root,
                skills=(),
                rejected=(
                    RejectedPackage(
                        package_name=root.name,
                        package_path=str(root),
                        reason=f"Safe platform security check failed for root: {exc}",
                        error_code="security_check_failed",
                    ),
                ),
            )
    elif hasattr(os, "O_NOFOLLOW"):
        try:
            fd_root, err_code, err_reason = _open_verified_root_posix(root)
            if fd_root is None:
                return SkillDiscoveryResult(
                    root_path=root,
                    skills=(),
                    rejected=(
                        RejectedPackage(
                            package_name=root.name,
                            package_path=str(root),
                            reason=err_reason or "Failed to open verified root descriptor.",
                            error_code=err_code or "root_access_error",
                        ),
                    ),
                )
        except Exception as exc:
            return SkillDiscoveryResult(
                root_path=root,
                skills=(),
                rejected=(
                    RejectedPackage(
                        package_name=root.name,
                        package_path=str(root),
                        reason=f"Safe platform security check failed for root: {exc}",
                        error_code="security_check_failed",
                    ),
                ),
            )
    else:
        return SkillDiscoveryResult(
            root_path=root,
            skills=(),
            rejected=(
                RejectedPackage(
                    package_name=root.name,
                    package_path=str(root),
                    reason="Platform does not support safe handle-relative or no-follow directory operations.",
                    error_code="security_check_failed",
                ),
            ),
        )

    try:
        # Enumerate candidate package directory entries anchored strictly to verified root handle / descriptor
        candidate_entries: list[tuple[str, bool, bool]] = []

        if h_root is not None:
            entries_win, err_code, err_reason = _enumerate_directory_entries_windows(h_root)
            if entries_win is None:
                return SkillDiscoveryResult(
                    root_path=root,
                    skills=(),
                    rejected=(
                        RejectedPackage(
                            package_name=root.name,
                            package_path=str(root),
                            reason=err_reason or "Failed to scan skills root via handle.",
                            error_code=err_code or "scan_error",
                        ),
                    ),
                )
            candidate_entries = entries_win
        elif fd_root is not None:
            try:
                if hasattr(os, "listdir") and os.listdir in getattr(os, "supports_fd", ()):
                    names = os.listdir(fd_root)
                else:
                    names = os.listdir(str(root))
                for name in names:
                    try:
                        st_child = os.stat(name, dir_fd=fd_root, follow_symlinks=False)
                        is_dir = stat.S_ISDIR(st_child.st_mode)
                        is_reparse = stat.S_ISLNK(st_child.st_mode)
                        candidate_entries.append((name, is_dir, is_reparse))
                    except OSError:
                        candidate_entries.append((name, False, False))
            except OSError as exc:
                return SkillDiscoveryResult(
                    root_path=root,
                    skills=(),
                    rejected=(
                        RejectedPackage(
                            package_name=root.name,
                            package_path=str(root),
                            reason=f"Failed to scan skills root: {exc}",
                            error_code="scan_error",
                        ),
                    ),
                )
        else:
            return SkillDiscoveryResult(
                root_path=root,
                skills=(),
                rejected=(
                    RejectedPackage(
                        package_name=root.name,
                        package_path=str(root),
                        reason="No verified root handle or descriptor available for enumeration.",
                        error_code="security_check_failed",
                    ),
                ),
            )

        # Deterministic alphabetical ordering by directory name
        candidate_entries.sort(key=lambda e: e[0])

        valid_skills: list[ExternalSkillProxy] = []
        rejected: list[RejectedPackage] = []
        seen_skill_ids: set[str] = set()
        candidate_dir_count: int = 0

        for pkg_name, is_directory, is_reparse in candidate_entries:
            # 1. Ignore unrelated regular files at root level (e.g. README.md, .gitignore)
            if not is_directory and not is_reparse:
                continue

            candidate_dir_count += 1
            pkg_path = root / pkg_name

            # 2. Strict resource limit: Maximum 100 package directories per scan
            if candidate_dir_count > MAX_PACKAGE_DIRECTORIES:
                rejected.append(
                    RejectedPackage(
                        package_name=pkg_name,
                        package_path=str(pkg_path),
                        reason=f"Maximum limit of {MAX_PACKAGE_DIRECTORIES} package directories exceeded.",
                        error_code="package_limit_exceeded",
                    )
                )
                continue

            # 3. Validate package directory name syntax and path traversal prevention
            if (
                ".." in pkg_name
                or "/" in pkg_name
                or "\\" in pkg_name
                or not PACKAGE_NAME_PATTERN.fullmatch(pkg_name)
            ):
                rejected.append(
                    RejectedPackage(
                        package_name=pkg_name,
                        package_path=str(pkg_path),
                        reason=f"Package directory name '{pkg_name}' contains invalid characters or traversal sequences.",
                        error_code="invalid_package_name",
                    )
                )
                continue

            # 4. Immediate symlink pre-check on package directory
            if is_reparse:
                rejected.append(
                    RejectedPackage(
                        package_name=pkg_name,
                        package_path=str(pkg_path),
                        reason=f"Package directory '{pkg_name}' is a symbolic link or reparse point.",
                        error_code="reparse_point_detected",
                    )
                )
                continue

            # 5. Read manifest bytes strictly relative to pinned root handle / descriptor
            if h_root is not None:
                try:
                    raw_bytes, err_code, err_reason = _read_manifest_bytes_windows(h_root, pkg_name)
                except Exception as exc:
                    rejected.append(
                        RejectedPackage(
                            package_name=pkg_name,
                            package_path=str(pkg_path),
                            reason=f"Safe platform security check failed: {exc}",
                            error_code="security_check_failed",
                        )
                    )
                    continue
            elif fd_root is not None:
                try:
                    raw_bytes, err_code, err_reason = _read_manifest_bytes_posix(fd_root, pkg_name)
                except Exception as exc:
                    rejected.append(
                        RejectedPackage(
                            package_name=pkg_name,
                            package_path=str(pkg_path),
                            reason=f"Safe platform security check failed: {exc}",
                            error_code="security_check_failed",
                        )
                    )
                    continue
            else:
                rejected.append(
                    RejectedPackage(
                        package_name=pkg_name,
                        package_path=str(pkg_path),
                        reason="No verified root handle or descriptor available.",
                        error_code="security_check_failed",
                    )
                )
                continue

            if raw_bytes is None:
                rejected.append(
                    RejectedPackage(
                        package_name=pkg_name,
                        package_path=str(pkg_path),
                        reason=err_reason or "Failed to read manifest safely.",
                        error_code=err_code or "manifest_read_error",
                    )
                )
                continue

            # 6. Parse and validate manifest schema
            try:
                manifest = parse_skill_manifest(raw_bytes)
            except ManifestValidationError as exc:
                rejected.append(
                    RejectedPackage(
                        package_name=pkg_name,
                        package_path=str(pkg_path),
                        reason=str(exc),
                        error_code="manifest_validation_error",
                    )
                )
                continue

            # 7. Enforce identity consistency: package directory 'weather' -> 'user.weather'
            expected_skill_id = f"user.{pkg_name}"
            if manifest.id != expected_skill_id:
                rejected.append(
                    RejectedPackage(
                        package_name=pkg_name,
                        package_path=str(pkg_path),
                        reason=(
                            f"Package directory '{pkg_name}' declares mismatched skill ID '{manifest.id}'. "
                            f"Expected '{expected_skill_id}'."
                        ),
                        error_code="identity_mismatch",
                    )
                )
                continue

            # 8. Duplicate skill ID rejection across packages
            if manifest.id in seen_skill_ids:
                rejected.append(
                    RejectedPackage(
                        package_name=pkg_name,
                        package_path=str(pkg_path),
                        reason=f"Duplicate skill ID '{manifest.id}' already discovered in this scan.",
                        error_code="duplicate_skill_id",
                    )
                )
                continue
            seen_skill_ids.add(manifest.id)

            # 9. Create metadata-only external skill proxy
            metadata = manifest.to_metadata(enabled=True)
            proxy = ExternalSkillProxy(
                metadata=metadata,
                package_path=pkg_path,
                manifest=manifest,
            )
            valid_skills.append(proxy)

        return SkillDiscoveryResult(
            root_path=root,
            skills=tuple(valid_skills),
            rejected=tuple(rejected),
        )
    finally:
        # Guarantee closure of pinned root handle / descriptor
        if h_root is not None and kernel32 is not None:
            kernel32.CloseHandle(wintypes.HANDLE(h_root))
        if fd_root is not None:
            os.close(fd_root)


def register_external_skills(
    registry: SkillRegistry,
    discovery_result: SkillDiscoveryResult,
    expected_generation: int | None = None,
) -> int:
    """Atomically register a complete validated external-skill snapshot with the registry.

    Guarantees:
        - Replaces previous external skill snapshot atomically under registry lock.
        - Monotonic generation check prevents stale snapshot overwrites.
        - Preserves all non-external / built-in skills.
        - Preserves enabled/disabled state of retained external skills.
        - Cleans up external skills that were removed or are now invalid.

    Returns:
        The total count of external skills in the active snapshot.
    """
    registry.update_external_skills(
        discovery_result.skills,
        expected_generation=expected_generation,
    )
    return len(discovery_result.skills)


def scan_and_register_external_skills(
    registry: SkillRegistry,
    root_path: Path | str | None = None,
) -> SkillDiscoveryResult:
    """Scan root_path and atomically update the registry with discovered external skills.

    Synchronization Guarantee:
        Serialized per registry via registry.scan_lock so concurrent discovery operations
        cannot publish out-of-order or overwrite a newer snapshot with stale results.
        Ordinary registry readers remain non-blocking and atomic.

    Returns:
        The SkillDiscoveryResult describing valid skills and rejected packages.
    """
    with registry.scan_lock:
        start_gen = registry.generation
        result = discover_external_skills(root_path=root_path)
        register_external_skills(registry, result, expected_generation=start_gen)
        return SkillDiscoveryResult(
            root_path=result.root_path,
            skills=result.skills,
            rejected=result.rejected,
            generation=registry.generation,
        )
