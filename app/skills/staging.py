"""Secure external skill package staging for sandboxed execution (Phase 10.4.3).

This module safely copies external skill packages from ~/.anaya/skills/ into per-execution
session scratch directories before starting isolated worker processes.

Guarantees:
    - Never gives worker processes direct access to ~/.anaya/skills/.
    - Reuses handle-relative traversal to prevent symlink, junction, and reparse-point escapes.
    - Compares pre- and post-read metadata to detect in-place modifications during staging.
    - Strictly bounds file count, file size, total package size, directory depth, and filename length.
    - Validates mandatory skill.json manifest and confirms the entrypoint module exists.
    - Prevents package files from shadowing trusted Anaya or standard library modules.
    - Sets read-only ACLs on staged Python files so the worker cannot modify executable code.
    - Staged files reside strictly inside session scratch and are reclaimed during session cleanup.
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
from typing import Any

from app.skills.manifest import (
    MAX_MANIFEST_BYTES,
    ManifestValidationError,
    SkillManifest,
    parse_skill_manifest,
)
from app.skills.discovery import (
    _enumerate_directory_entries_windows,
    _is_dos_device_mapping,
    _open_verified_root_posix,
    _open_verified_root_windows,
)

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes
    from app.skills.sandbox.win32_acl import (
        NativeApiError,
        build_explicit_access,
        convert_string_to_sid,
        grant_path_access,
        open_relative_child_handle,
    )
    from app.skills.sandbox.win32_acl_types import (
        FILE_ATTRIBUTE_DIRECTORY,
        FILE_ATTRIBUTE_REPARSE_POINT,
        FILE_ATTRIBUTE_TAG_INFO,
        FILE_BASIC_INFO,
        FILE_GENERIC_READ,
        FILE_READ_ATTRIBUTES,
        FileAttributeTagInfo,
        FileBasicInfo,
        RUNTIME_ACCESS_MASK,
        FILE_ALL_ACCESS,
        SUB_CONTAINERS_AND_OBJECTS_INHERIT,
        SYNCHRONIZE,
        kernel32,
        advapi32,
    )
    from app.skills.sandbox.runtime_provisioner import (
        SID_ADMINISTRATORS,
        SID_LOCAL_SYSTEM,
        get_current_user_sid_string,
    )

    FILE_ATTRIBUTE_DEVICE: int = 0x00000040
    FILE_LIST_DIRECTORY: int = 0x0001
else:
    ctypes = None  # type: ignore[assignment]
    wintypes = None  # type: ignore[assignment]
    kernel32 = None  # type: ignore[assignment]
    advapi32 = None  # type: ignore[assignment]

logger = logging.getLogger("anaya.skills.staging")

# Conservative Resource Limits
MAX_STAGED_FILES: int = 50
MAX_SOURCE_FILE_BYTES: int = 256 * 1024  # 256 KiB individual file limit
MAX_TOTAL_PACKAGE_BYTES: int = 2 * 1024 * 1024  # 2 MiB package limit
MAX_STAGING_DEPTH: int = 3
MAX_FILENAME_LENGTH: int = 64

# Allowed file extensions in a staged package
ALLOWED_EXTENSIONS: frozenset[str] = frozenset({".py", ".json"})

# Safe filename pattern (alphanumerics, dots, underscores, hyphens)
SAFE_FILENAME_PATTERN: re.Pattern[str] = re.compile(r"^[a-zA-Z0-9_.-]+$")

# Protected module names that package code must never shadow
PROTECTED_MODULE_NAMES: frozenset[str] = frozenset({
    "app",
    "sys",
    "os",
    "json",
    "math",
    "re",
    "logging",
    "ctypes",
    "builtins",
    "importlib",
    "subprocess",
    "socket",
    "threading",
    "time",
    "types",
    "typing",
    "collections",
    "io",
    "pathlib",
    "dataclasses",
    "enum",
    "errno",
    "stat",
    "shutil",
    "copy",
})


class StagingError(Exception):
    """Base exception for all external skill staging errors."""

    def __init__(self, message: str, error_code: str = "staging_error") -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code


@dataclass(frozen=True)
class StagedPackage:
    """Immutable metadata describing a safely staged external skill package."""

    skill_id: str
    manifest: SkillManifest
    staged_dir: Path
    entrypoint_module: str
    entrypoint_class: str
    file_count: int
    total_bytes: int


def _validate_filename(name: str) -> None:
    """Validate filename length, character set, and absence of path traversal."""
    if not isinstance(name, str) or not name.strip():
        raise StagingError("Filename must be a non-empty string.", "invalid_filename")
    if len(name) > MAX_FILENAME_LENGTH:
        raise StagingError(
            f"Filename '{name[:32]}...' exceeds maximum length of {MAX_FILENAME_LENGTH} characters.",
            "invalid_filename",
        )
    if ".." in name or "/" in name or "\\" in name:
        raise StagingError(
            f"Filename '{name}' contains path traversal or separator characters.",
            "path_traversal_detected",
        )
    if not SAFE_FILENAME_PATTERN.fullmatch(name):
        raise StagingError(
            f"Filename '{name}' contains disallowed characters. Only alphanumerics, dots, underscores, and hyphens are permitted.",
            "invalid_filename",
        )


def _check_shadowing(name: str) -> None:
    """Reject filenames or module names that attempt to shadow protected modules."""
    base_name = name.split(".")[0].lower()
    if base_name in PROTECTED_MODULE_NAMES:
        raise StagingError(
            f"Package component '{name}' attempts to shadow protected module '{base_name}'.",
            "shadowing_disallowed",
        )


def _read_file_bytes_windows(
    h_dir: int,
    filename: str,
) -> tuple[bytes, int]:
    """Read regular file handle-relatively on Windows with before/after mutation detection."""
    if kernel32 is None:  # pragma: no cover
        raise StagingError("Windows kernel32 is unavailable.", "platform_unavailable")

    h_file = open_relative_child_handle(
        h_dir,
        filename,
        desired_access=FILE_GENERIC_READ | FILE_READ_ATTRIBUTES,
    )
    try:
        fati = FILE_ATTRIBUTE_TAG_INFO()
        if not kernel32.GetFileInformationByHandleEx(
            wintypes.HANDLE(h_file),
            FileAttributeTagInfo,
            ctypes.byref(fati),
            ctypes.sizeof(fati),
        ):
            raise StagingError(f"Failed to query attributes for '{filename}'.", "file_access_error")

        if fati.FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
            raise StagingError(
                f"File '{filename}' is a reparse point or symlink.",
                "reparse_point_detected",
            )
        if fati.FileAttributes & FILE_ATTRIBUTE_DIRECTORY:
            raise StagingError(f"Expected regular file but '{filename}' is a directory.", "invalid_file_type")
        if fati.FileAttributes & FILE_ATTRIBUTE_DEVICE:
            raise StagingError(f"File '{filename}' is a device, not a regular file.", "invalid_file_type")

        # Initial size and timestamp check
        basic_before = FILE_BASIC_INFO()
        if not kernel32.GetFileInformationByHandleEx(
            wintypes.HANDLE(h_file),
            FileBasicInfo,
            ctypes.byref(basic_before),
            ctypes.sizeof(basic_before),
        ):
            raise StagingError(f"Failed to query timestamps for '{filename}'.", "file_access_error")

        file_size_before = wintypes.LARGE_INTEGER()
        if not kernel32.GetFileSizeEx(wintypes.HANDLE(h_file), ctypes.byref(file_size_before)):
            raise StagingError(f"Failed to query size for '{filename}'.", "file_access_error")

        initial_size = file_size_before.value
        if initial_size > MAX_SOURCE_FILE_BYTES:
            raise StagingError(
                f"File '{filename}' size ({initial_size} B) exceeds maximum allowed limit of {MAX_SOURCE_FILE_BYTES} B.",
                "file_too_large",
            )

        buf = ctypes.create_string_buffer(initial_size + 1)
        bytes_read = wintypes.DWORD()
        if not kernel32.ReadFile(wintypes.HANDLE(h_file), buf, initial_size + 1, ctypes.byref(bytes_read), None):
            raise StagingError(f"Failed to read file '{filename}'.", "file_read_error")

        actual_read = bytes_read.value
        if actual_read > MAX_SOURCE_FILE_BYTES:
            raise StagingError(f"File '{filename}' read exceeded maximum limit.", "file_too_large")

        # Post-read verification
        basic_after = FILE_BASIC_INFO()
        if not kernel32.GetFileInformationByHandleEx(
            wintypes.HANDLE(h_file),
            FileBasicInfo,
            ctypes.byref(basic_after),
            ctypes.sizeof(basic_after),
        ):
            raise StagingError(f"Failed to query post-read timestamps for '{filename}'.", "file_access_error")

        file_size_after = wintypes.LARGE_INTEGER()
        if not kernel32.GetFileSizeEx(wintypes.HANDLE(h_file), ctypes.byref(file_size_after)):
            raise StagingError(f"Failed to query post-read size for '{filename}'.", "file_access_error")

        post_size = file_size_after.value
        if (
            post_size != initial_size
            or actual_read != initial_size
            or basic_after.LastWriteTime != basic_before.LastWriteTime
            or basic_after.ChangeTime != basic_before.ChangeTime
        ):
            raise StagingError(
                f"File '{filename}' was modified during staging (size before: {initial_size}, read: {actual_read}, after: {post_size}).",
                "package_modified_during_staging",
            )

        return bytes(buf.raw[:actual_read]), actual_read
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(h_file))


def _read_file_bytes_posix(
    fd_dir: int,
    filename: str,
) -> tuple[bytes, int]:
    """Read regular file descriptor-relatively on POSIX with mutation detection."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd_file = os.open(filename, flags, dir_fd=fd_dir)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EEXIST):
            raise StagingError(f"File '{filename}' is a symbolic link.", "reparse_point_detected") from exc
        raise StagingError(f"Failed to open '{filename}': {exc}", "file_access_error") from exc

    try:
        st_before = os.fstat(fd_file)
        if not stat.S_ISREG(st_before.st_mode):
            raise StagingError(f"'{filename}' is not a regular file.", "invalid_file_type")
        if st_before.st_size > MAX_SOURCE_FILE_BYTES:
            raise StagingError(
                f"File '{filename}' size ({st_before.st_size} B) exceeds maximum limit of {MAX_SOURCE_FILE_BYTES} B.",
                "file_too_large",
            )

        content = os.read(fd_file, MAX_SOURCE_FILE_BYTES + 1)
        if len(content) > MAX_SOURCE_FILE_BYTES:
            raise StagingError(f"File '{filename}' payload exceeded limit.", "file_too_large")

        st_after = os.fstat(fd_file)
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
            raise StagingError(
                f"File '{filename}' was modified during staging.",
                "package_modified_during_staging",
            )

        return content, len(content)
    finally:
        os.close(fd_file)


def _collect_package_files_windows(
    h_pkg: int,
    pkg_name: str,
) -> dict[str, bytes]:
    """Recursively collect and validate all files in package handle-relatively on Windows."""
    collected: dict[str, bytes] = {}
    total_bytes = 0

    # Stack: (relative_dir_path, h_open_dir, depth)
    # Root package dir handle is borrowed and closed by caller
    stack: list[tuple[str, int, int, bool]] = [("", h_pkg, 0, False)]

    try:
        while stack:
            rel_dir, h_curr, depth, must_close = stack.pop()
            try:
                entries, err_code, reason = _enumerate_directory_entries_windows(h_curr)
                if entries is None:
                    raise StagingError(
                        f"Directory enumeration failed for '{rel_dir or pkg_name}': {reason}",
                        err_code or "directory_scan_error",
                    )

                for entry_name, is_dir, is_reparse in entries:
                    _validate_filename(entry_name)
                    _check_shadowing(entry_name)

                    child_rel_path = f"{rel_dir}/{entry_name}".lstrip("/") if rel_dir else entry_name

                    if is_reparse:
                        raise StagingError(
                            f"Reparse point or symlink detected at '{child_rel_path}'.",
                            "reparse_point_detected",
                        )

                    if is_dir:
                        if depth + 1 > MAX_STAGING_DEPTH:
                            raise StagingError(
                                f"Package directory depth exceeded at '{child_rel_path}'. Maximum depth is {MAX_STAGING_DEPTH}.",
                                "directory_depth_exceeded",
                            )

                        h_sub = open_relative_child_handle(
                            h_curr,
                            entry_name,
                            desired_access=FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
                        )
                        # Verify subdirectory attributes
                        fati_sub = FILE_ATTRIBUTE_TAG_INFO()
                        if not kernel32.GetFileInformationByHandleEx(
                            wintypes.HANDLE(h_sub),
                            FileAttributeTagInfo,
                            ctypes.byref(fati_sub),
                            ctypes.sizeof(fati_sub),
                        ):
                            kernel32.CloseHandle(wintypes.HANDLE(h_sub))
                            raise StagingError(f"Failed to query attributes for '{child_rel_path}'.", "directory_access_error")

                        if fati_sub.FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
                            kernel32.CloseHandle(wintypes.HANDLE(h_sub))
                            raise StagingError(f"Directory '{child_rel_path}' is a reparse point.", "reparse_point_detected")

                        if not (fati_sub.FileAttributes & FILE_ATTRIBUTE_DIRECTORY):
                            kernel32.CloseHandle(wintypes.HANDLE(h_sub))
                            raise StagingError(f"Path '{child_rel_path}' is not a directory.", "invalid_directory")

                        stack.append((child_rel_path, h_sub, depth + 1, True))
                    else:
                        ext = Path(entry_name).suffix.lower()
                        if ext not in ALLOWED_EXTENSIONS:
                            raise StagingError(
                                f"File '{child_rel_path}' has disallowed extension '{ext}'. Only .py and .json are permitted.",
                                "disallowed_file_extension",
                            )

                        if len(collected) + 1 > MAX_STAGED_FILES:
                            raise StagingError(
                                f"Package file count exceeded maximum limit of {MAX_STAGED_FILES} files.",
                                "file_count_exceeded",
                            )

                        file_bytes, f_size = _read_file_bytes_windows(h_curr, entry_name)
                        total_bytes += f_size
                        if total_bytes > MAX_TOTAL_PACKAGE_BYTES:
                            raise StagingError(
                                f"Total package size ({total_bytes} B) exceeded limit of {MAX_TOTAL_PACKAGE_BYTES} B.",
                                "package_too_large",
                            )

                        collected[child_rel_path] = file_bytes
            finally:
                if must_close:
                    kernel32.CloseHandle(wintypes.HANDLE(h_curr))

        return collected
    except Exception:
        # Drain and close any remaining handles on stack
        while stack:
            _, h_rem, _, must_close_rem = stack.pop()
            if must_close_rem:
                kernel32.CloseHandle(wintypes.HANDLE(h_rem))
        raise


def _collect_package_files_posix(
    fd_pkg: int,
    pkg_name: str,
) -> dict[str, bytes]:
    """Recursively collect and validate all files in package descriptor-relatively on POSIX."""
    collected: dict[str, bytes] = {}
    total_bytes = 0

    stack: list[tuple[str, int, int, bool]] = [("", fd_pkg, 0, False)]

    try:
        while stack:
            rel_dir, fd_curr, depth, must_close = stack.pop()
            try:
                if hasattr(os, "listdir") and os.listdir in getattr(os, "supports_fd", ()):
                    entries = os.listdir(fd_curr)
                else:
                    raise StagingError("Platform lacks fd-relative directory listing.", "platform_unavailable")

                for entry_name in entries:
                    _validate_filename(entry_name)
                    _check_shadowing(entry_name)

                    child_rel_path = f"{rel_dir}/{entry_name}".lstrip("/") if rel_dir else entry_name

                    st = os.stat(entry_name, dir_fd=fd_curr, follow_symlinks=False)
                    if stat.S_ISLNK(st.st_mode):
                        raise StagingError(
                            f"Symbolic link detected at '{child_rel_path}'.",
                            "reparse_point_detected",
                        )

                    if stat.S_ISDIR(st.st_mode):
                        if depth + 1 > MAX_STAGING_DEPTH:
                            raise StagingError(
                                f"Package directory depth exceeded at '{child_rel_path}'.",
                                "directory_depth_exceeded",
                            )

                        flags_dir = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
                        fd_sub = os.open(entry_name, flags_dir, dir_fd=fd_curr)
                        stack.append((child_rel_path, fd_sub, depth + 1, True))
                    elif stat.S_ISREG(st.st_mode):
                        ext = Path(entry_name).suffix.lower()
                        if ext not in ALLOWED_EXTENSIONS:
                            raise StagingError(
                                f"File '{child_rel_path}' has disallowed extension '{ext}'.",
                                "disallowed_file_extension",
                            )

                        if len(collected) + 1 > MAX_STAGED_FILES:
                            raise StagingError(
                                f"Package file count exceeded maximum limit of {MAX_STAGED_FILES}.",
                                "file_count_exceeded",
                            )

                        file_bytes, f_size = _read_file_bytes_posix(fd_curr, entry_name)
                        total_bytes += f_size
                        if total_bytes > MAX_TOTAL_PACKAGE_BYTES:
                            raise StagingError(
                                f"Total package size exceeded limit of {MAX_TOTAL_PACKAGE_BYTES} B.",
                                "package_too_large",
                            )

                        collected[child_rel_path] = file_bytes
                    else:
                        raise StagingError(
                            f"File '{child_rel_path}' is not a regular file.",
                            "invalid_file_type",
                        )
            finally:
                if must_close:
                    os.close(fd_curr)

        return collected
    except Exception:
        while stack:
            _, fd_rem, _, must_close_rem = stack.pop()
            if must_close_rem:
                os.close(fd_rem)
        raise


def _apply_read_only_acl_windows(
    staged_dir: Path,
    worker_sid: str,
) -> None:
    """Apply Read & Execute DACL to staged package directory, preventing worker write/delete."""
    if sys.platform != "win32" or advapi32 is None or kernel32 is None:
        return

    current_user_sid = get_current_user_sid_string()
    sids_to_free: list[int] = []

    try:
        p_worker_sid = convert_string_to_sid(worker_sid)
        sids_to_free.append(p_worker_sid)

        p_user = convert_string_to_sid(current_user_sid)
        sids_to_free.append(p_user)

        p_admin = convert_string_to_sid(SID_ADMINISTRATORS)
        sids_to_free.append(p_admin)

        p_system = convert_string_to_sid(SID_LOCAL_SYSTEM)
        sids_to_free.append(p_system)

        # Full control for host supervisor, admin, system
        ea_user = build_explicit_access(p_user, FILE_ALL_ACCESS, SUB_CONTAINERS_AND_OBJECTS_INHERIT)
        ea_admin = build_explicit_access(p_admin, FILE_ALL_ACCESS, SUB_CONTAINERS_AND_OBJECTS_INHERIT)
        ea_system = build_explicit_access(p_system, FILE_ALL_ACCESS, SUB_CONTAINERS_AND_OBJECTS_INHERIT)

        # Read & Execute strictly for worker SID
        ea_worker = build_explicit_access(
            p_worker_sid,
            RUNTIME_ACCESS_MASK,
            SUB_CONTAINERS_AND_OBJECTS_INHERIT,
        )

        grant_path_access(
            staged_dir,
            [ea_user, ea_admin, ea_system, ea_worker],
            protect_dacl=True,
            replace_dacl=True,
        )
    finally:
        for p_sid in sids_to_free:
            kernel32.LocalFree(ctypes.c_void_p(p_sid))


def stage_external_package(
    package_source_path: Path | str,
    session_scratch_dir: Path | str,
    worker_sid: str | None = None,
    skills_root: Path | str | None = None,
) -> StagedPackage:
    """Safely verify and stage an external skill package into a session scratch directory.

    Guarantees:
        - Pins root and package handles to avoid TOCTOU directory replacement.
        - Rejects reparse points, junctions, and symlinks at all levels.
        - Reads files handle-relatively with pre/post timestamp & size verification.
        - Rejects package source modified during staging.
        - Validates manifest schema and ensures declared entrypoint module is present.
        - Stages package snapshot into session_scratch_dir / "package".
        - Protects staged code with Read & Execute ACL on Windows.
    """
    pkg_source = Path(package_source_path).resolve()
    scratch_dir = Path(session_scratch_dir).resolve()

    if not scratch_dir.is_dir():
        raise StagingError(f"Session scratch directory '{scratch_dir}' does not exist.", "scratch_dir_not_found")

    pkg_name = pkg_source.name
    _validate_filename(pkg_name)

    effective_root = Path(skills_root).resolve() if skills_root is not None else pkg_source.parent

    # 1. Open verified root and package handle / descriptor
    collected_files: dict[str, bytes] = {}

    if sys.platform == "win32" and kernel32 is not None:
        h_root, err_code, reason = _open_verified_root_windows(effective_root)
        if h_root is None:
            raise StagingError(f"Failed to open verified skills root: {reason}", err_code or "root_access_error")

        try:
            try:
                h_pkg = open_relative_child_handle(
                    h_root,
                    pkg_name,
                    desired_access=FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
                )
            except NativeApiError as exc:
                raise StagingError(f"Failed to open package directory '{pkg_name}': {exc}", "package_access_error") from exc

            try:
                fati_pkg = FILE_ATTRIBUTE_TAG_INFO()
                if not kernel32.GetFileInformationByHandleEx(
                    wintypes.HANDLE(h_pkg),
                    FileAttributeTagInfo,
                    ctypes.byref(fati_pkg),
                    ctypes.sizeof(fati_pkg),
                ):
                    raise StagingError(f"Failed to retrieve attributes for '{pkg_name}'.", "package_access_error")

                if fati_pkg.FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
                    raise StagingError(
                        f"Package directory '{pkg_name}' is a reparse point or symlink.",
                        "reparse_point_detected",
                    )
                if not (fati_pkg.FileAttributes & FILE_ATTRIBUTE_DIRECTORY):
                    raise StagingError(f"Package '{pkg_name}' is not a directory.", "not_a_directory")

                collected_files = _collect_package_files_windows(h_pkg, pkg_name)
            finally:
                kernel32.CloseHandle(wintypes.HANDLE(h_pkg))
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(h_root))
    elif hasattr(os, "O_NOFOLLOW"):
        fd_root, err_code, reason = _open_verified_root_posix(effective_root)
        if fd_root is None:
            raise StagingError(f"Failed to open verified skills root: {reason}", err_code or "root_access_error")

        try:
            flags_dir = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
            try:
                fd_pkg = os.open(pkg_name, flags_dir, dir_fd=fd_root)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.EEXIST):
                    raise StagingError(f"Package '{pkg_name}' is a symlink.", "reparse_point_detected") from exc
                raise StagingError(f"Failed to open package '{pkg_name}': {exc}", "package_access_error") from exc

            try:
                st = os.fstat(fd_pkg)
                if stat.S_ISLNK(st.st_mode):
                    raise StagingError(f"Package '{pkg_name}' is a symlink.", "reparse_point_detected")
                if not stat.S_ISDIR(st.st_mode):
                    raise StagingError(f"Package '{pkg_name}' is not a directory.", "not_a_directory")

                collected_files = _collect_package_files_posix(fd_pkg, pkg_name)
            finally:
                os.close(fd_pkg)
        finally:
            os.close(fd_root)
    else:
        raise StagingError("Platform lacks required safe handle/descriptor primitives.", "platform_unavailable")

    # 2. Verify mandatory skill.json manifest
    if "skill.json" not in collected_files:
        raise StagingError("Mandatory manifest 'skill.json' not found in package.", "missing_manifest")

    try:
        manifest = parse_skill_manifest(collected_files["skill.json"])
    except ManifestValidationError as exc:
        raise StagingError(f"Invalid skill manifest: {exc}", "manifest_validation_error") from exc

    expected_id = f"user.{pkg_name}"
    if manifest.id != expected_id:
        raise StagingError(
            f"Package directory '{pkg_name}' declares mismatched skill ID '{manifest.id}' (expected '{expected_id}').",
            "identity_mismatch",
        )

    # 3. Verify entrypoint module exists in collected package files
    entrypoint_parts = manifest.entrypoint.split(":")
    module_name = entrypoint_parts[0]
    class_name = entrypoint_parts[1]

    expected_module_file = f"{module_name}.py"
    expected_package_file = f"{module_name}/__init__.py"
    if expected_module_file not in collected_files and expected_package_file not in collected_files:
        raise StagingError(
            f"Entrypoint module '{module_name}' declared in skill.json not found in package files.",
            "entrypoint_not_found",
        )

    # 4. Stage files into session_scratch_dir / "package"
    target_staged_dir = scratch_dir / "package"
    if target_staged_dir.exists():
        raise StagingError(f"Target staged directory '{target_staged_dir}' already exists.", "staging_conflict")

    target_staged_dir.mkdir(parents=True, exist_ok=False)

    try:
        for rel_file, data in collected_files.items():
            dest_file = target_staged_dir / rel_file
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            dest_file.write_bytes(data)

        # 5. Apply Read & Execute permissions to staged code
        if sys.platform == "win32" and worker_sid is not None:
            _apply_read_only_acl_windows(target_staged_dir, worker_sid)
        else:
            # POSIX read-only: files 0o444, dirs 0o555
            for root_d, dirs, files in os.walk(target_staged_dir):
                for d in dirs:
                    try:
                        os.chmod(os.path.join(root_d, d), 0o555)
                    except Exception:
                        pass
                for f in files:
                    try:
                        os.chmod(os.path.join(root_d, f), 0o444)
                    except Exception:
                        pass
            try:
                os.chmod(target_staged_dir, 0o555)
            except Exception:
                pass

        total_bytes = sum(len(d) for d in collected_files.values())
        return StagedPackage(
            skill_id=manifest.id,
            manifest=manifest,
            staged_dir=target_staged_dir,
            entrypoint_module=module_name,
            entrypoint_class=class_name,
            file_count=len(collected_files),
            total_bytes=total_bytes,
        )
    except Exception:
        # Cleanup partially staged directory on failure
        shutil.rmtree(target_staged_dir, ignore_errors=True)
        raise
