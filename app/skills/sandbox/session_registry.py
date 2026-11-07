"""Crash-safe, persistent session registry and exclusive locking for sandbox workers.

Provides permanent tombstone synchronization, write-ahead metadata transitions,
atomic file replacement, strict schema validation, and PID + creation FILETIME process
validation to prevent PID reuse.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
from enum import Enum
import json
import logging
import os
from pathlib import Path
import sys
from typing import Any

from app.skills.sandbox.appcontainer import InvalidSessionIdError, validate_session_id
from app.skills.sandbox.win32_acl_types import (
    CREATE_ALWAYS,
    CREATE_NEW,
    DELETE,
    ERROR_ACCESS_DENIED,
    ERROR_FILE_EXISTS,
    ERROR_FILE_NOT_FOUND,
    ERROR_INVALID_PARAMETER,
    ERROR_SHARING_VIOLATION,
    ERROR_SUCCESS,
    FILE_ATTRIBUTE_DIRECTORY,
    FILE_ATTRIBUTE_NORMAL,
    FILE_ATTRIBUTE_REPARSE_POINT,
    FILE_ATTRIBUTE_TAG_INFO,
    FILE_FLAG_BACKUP_SEMANTICS,
    FILE_FLAG_OPEN_REPARSE_POINT,
    FILE_READ_ATTRIBUTES,
    FILE_SHARE_DELETE,
    FILE_SHARE_READ,
    FILE_SHARE_WRITE,
    FILE_WRITE_ATTRIBUTES,
    FileAttributeTagInfo,
    GENERIC_READ,
    GENERIC_WRITE,
    OPEN_EXISTING,
    SYNCHRONIZE,
    is_invalid_handle,
    kernel32,
    userenv,
)
from app.skills.sandbox.win32_acl import (
    NativeApiError,
    clean_directory_handle_relative,
    delete_file_by_handle,
    open_relative_child_handle,
)

logger = logging.getLogger("anaya.skills.sandbox.session_registry")

# Standard metadata statuses
STATUS_PROFILE_PENDING: str = "profile_pending"
STATUS_PROFILE_CREATED: str = "profile_created"
STATUS_SCRATCH_READY: str = "scratch_ready"
STATUS_WORKER_SUSPENDED: str = "worker_suspended"
STATUS_WORKER_RUNNING: str = "worker_running"
STATUS_COMPLETED: str = "completed"

VALID_STATUSES: frozenset[str] = frozenset(
    {
        STATUS_PROFILE_PENDING,
        STATUS_PROFILE_CREATED,
        STATUS_SCRATCH_READY,
        STATUS_WORKER_SUSPENDED,
        STATUS_WORKER_RUNNING,
        STATUS_COMPLETED,
    }
)

# Maximum allowed metadata file size (64 KiB)
MAX_METADATA_BYTES: int = 65536

# Strict allowlist of permitted metadata schema fields
ALLOWED_METADATA_FIELDS: frozenset[str] = frozenset(
    {
        "session_id",
        "profile_name",
        "supervisor_pid",
        "supervisor_start_time",
        "worker_pid",
        "worker_start_time",
        "status",
        "scratch_path",
        "created_at_utc",
    }
)


class SessionRegistryError(Exception):
    """Base exception for all session registry errors."""


class SessionLockError(SessionRegistryError):
    """Raised when an exclusive session lock cannot be acquired or held."""


class SessionMetadataError(SessionRegistryError):
    """Raised when session metadata is invalid, missing, or corrupt."""


class SessionActiveError(SessionRegistryError):
    """Raised when attempting to reclaim or alter an actively locked session."""


class ProcessLiveness(Enum):
    """Tri-state process liveness evaluation."""
    ALIVE = "alive"
    DEAD = "dead"
    UNKNOWN = "unknown"


def _reject_duplicate_metadata_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """JSON object_pairs_hook that strictly rejects duplicate keys."""
    res: dict[str, Any] = {}
    for k, v in pairs:
        if k in res:
            raise SessionMetadataError(f"Duplicate metadata JSON key detected: '{k}'.")
        res[k] = v
    return res


def validate_metadata_schema(metadata: Any, expected_session_id: str | None = None) -> None:
    """Strictly validate session metadata structure, types, ranges, and schema invariants."""
    if not isinstance(metadata, dict):
        raise SessionMetadataError("Metadata must be a dictionary.")

    keys = set(metadata.keys())
    unexpected = keys - ALLOWED_METADATA_FIELDS
    if unexpected:
        raise SessionMetadataError(f"Unexpected metadata field(s) detected: {sorted(unexpected)}.")

    # Required fields
    for req in ("session_id", "status", "profile_name"):
        if req not in metadata:
            raise SessionMetadataError(f"Missing required metadata field: '{req}'.")

    session_id = metadata["session_id"]
    if not isinstance(session_id, str):
        raise SessionMetadataError("session_id must be a string.")
    validated_id = validate_session_id(session_id)
    if expected_session_id is not None and validated_id != expected_session_id:
        raise SessionMetadataError(
            f"Metadata session_id '{session_id}' does not match expected '{expected_session_id}'."
        )

    profile_name = metadata["profile_name"]
    if not isinstance(profile_name, str):
        raise SessionMetadataError("profile_name must be a string.")
    expected_profile = f"Anaya.Worker.{validated_id}"
    if profile_name != expected_profile:
        raise SessionMetadataError(
            f"Invalid profile_name '{profile_name}'. Must be exactly '{expected_profile}'."
        )

    status = metadata["status"]
    if status not in VALID_STATUSES:
        raise SessionMetadataError(f"Invalid metadata status: '{status}'.")

    # supervisor_pid & supervisor_start_time
    sup_pid = metadata.get("supervisor_pid")
    if sup_pid is not None:
        if not isinstance(sup_pid, int) or isinstance(sup_pid, bool) or sup_pid < 1 or sup_pid > 4194304:
            raise SessionMetadataError(f"Invalid supervisor_pid: {sup_pid!r}.")

    sup_time = metadata.get("supervisor_start_time")
    if sup_time is not None:
        if not isinstance(sup_time, int) or isinstance(sup_time, bool) or sup_time <= 0 or sup_time > 0xFFFFFFFFFFFFFFFF:
            raise SessionMetadataError(f"Invalid supervisor_start_time: {sup_time!r}.")

    # worker_pid & worker_start_time
    w_pid = metadata.get("worker_pid")
    if w_pid is not None:
        if not isinstance(w_pid, int) or isinstance(w_pid, bool) or w_pid < 1 or w_pid > 4194304:
            raise SessionMetadataError(f"Invalid worker_pid: {w_pid!r}.")

    w_time = metadata.get("worker_start_time")
    if w_time is not None:
        if not isinstance(w_time, int) or isinstance(w_time, bool) or w_time <= 0 or w_time > 0xFFFFFFFFFFFFFFFF:
            raise SessionMetadataError(f"Invalid worker_start_time: {w_time!r}.")

    # scratch_path
    scratch_path = metadata.get("scratch_path")
    if scratch_path is not None and not isinstance(scratch_path, str):
        raise SessionMetadataError(f"Invalid scratch_path type: {type(scratch_path).__name__}.")

    # created_at_utc
    created_at = metadata.get("created_at_utc")
    if created_at is not None:
        if not isinstance(created_at, str):
            raise SessionMetadataError(f"Invalid created_at_utc type: {type(created_at).__name__}.")
        try:
            datetime.fromisoformat(created_at)
        except Exception as err:
            raise SessionMetadataError(f"Invalid created_at_utc ISO timestamp '{created_at}': {err}") from err


def get_process_creation_time(pid: int) -> int | None:
    """Retrieve the 64-bit creation FILETIME of a process by PID.

    Returns the integer timestamp (100-nanosecond intervals since Jan 1 1601) or None.
    """
    if pid <= 0 or sys.platform != "win32" or kernel32 is None:
        return None

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h_proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, wintypes.DWORD(pid))
    if is_invalid_handle(h_proc):
        return None

    try:
        creation = wintypes.LARGE_INTEGER()
        exit_time = wintypes.LARGE_INTEGER()
        kernel_time = wintypes.LARGE_INTEGER()
        user_time = wintypes.LARGE_INTEGER()
        ok = kernel32.GetProcessTimes(
            h_proc,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        )
        if not ok:
            return None
        return int(creation.value)
    finally:
        kernel32.CloseHandle(h_proc)


def check_process_liveness(pid: int, expected_creation_time: int | None) -> ProcessLiveness:
    """Conservatively determine whether a process is alive, dead, or unknown.

    Returns:
    - ALIVE: OpenProcess and GetProcessTimes succeeded, and creation time matched.
    - DEAD: PID <= 0, or OpenProcess failed with ERROR_INVALID_PARAMETER (PID does not exist),
      or PID exists but creation time does not match (PID was recycled).
    - UNKNOWN: OpenProcess failed with ERROR_ACCESS_DENIED, GetProcessTimes failed, or
      any unexpected native error occurred. UNKNOWN must always block cleanup.
    """
    if pid <= 0:
        return ProcessLiveness.DEAD

    if sys.platform != "win32" or kernel32 is None:
        return ProcessLiveness.UNKNOWN

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h_proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, wintypes.DWORD(pid))
    if is_invalid_handle(h_proc):
        err = ctypes.get_last_error()
        if err == ERROR_INVALID_PARAMETER:
            return ProcessLiveness.DEAD
        return ProcessLiveness.UNKNOWN

    try:
        creation = wintypes.LARGE_INTEGER()
        exit_time = wintypes.LARGE_INTEGER()
        kernel_time = wintypes.LARGE_INTEGER()
        user_time = wintypes.LARGE_INTEGER()
        ok = kernel32.GetProcessTimes(
            h_proc,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        )
        if not ok:
            return ProcessLiveness.UNKNOWN

        actual_creation = int(creation.value)
        if expected_creation_time is not None and actual_creation != expected_creation_time:
            # PID was recycled by the operating system for a different process
            return ProcessLiveness.DEAD

        return ProcessLiveness.ALIVE
    finally:
        kernel32.CloseHandle(h_proc)


def is_process_alive(pid: int, expected_creation_time: int | None) -> bool:
    """Conservative boolean process check. Returns True if ALIVE or UNKNOWN (fails closed)."""
    return check_process_liveness(pid, expected_creation_time) != ProcessLiveness.DEAD


def verify_ordinary_directory_not_reparse(path: Path | str) -> bool:
    """Verify via reparse-point protected handle that path is an ordinary directory.

    Guarantees:
    - Path is opened with FILE_FLAG_OPEN_REPARSE_POINT and FILE_FLAG_BACKUP_SEMANTICS.
    - If path is a reparse point (junction, symlink, mount point), returns False.
    - If path is not a directory, returns False.
    - If path does not exist, returns True (already absent, safe for cleanup).
    - Handle is closed exactly once.
    """
    if sys.platform != "win32" or kernel32 is None:
        p = Path(path)
        if not p.exists():
            return True
        return p.is_dir() and not p.is_symlink()

    h = kernel32.CreateFileW(
        str(path),
        FILE_READ_ATTRIBUTES | SYNCHRONIZE,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None,
        OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if is_invalid_handle(h):
        err = ctypes.get_last_error()
        if err in (ERROR_FILE_NOT_FOUND, 3):  # ERROR_FILE_NOT_FOUND or ERROR_PATH_NOT_FOUND
            return True
        return False

    try:
        fati = FILE_ATTRIBUTE_TAG_INFO()
        ok = kernel32.GetFileInformationByHandleEx(
            wintypes.HANDLE(h),
            FileAttributeTagInfo,
            ctypes.byref(fati),
            ctypes.sizeof(fati),
        )
        if not ok:
            return False

        attrs = fati.FileAttributes
        if attrs & FILE_ATTRIBUTE_REPARSE_POINT:
            return False
        if not (attrs & FILE_ATTRIBUTE_DIRECTORY):
            return False
        return True
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(h))


def open_verified_session_scratch_handle(
    scratch_base: Path | str,
    session_id: str,
    desired_access: int = DELETE | SYNCHRONIZE | FILE_READ_ATTRIBUTES | FILE_WRITE_ATTRIBUTES | GENERIC_READ,
) -> tuple[str, int | None]:
    """Verify scratch root and every ancestor directory handle-relatively, returning open session handle.

    Guarantees:
    - Normalizes the base scratch directory as an absolute lexical path without following symlinks.
    - Traverses handle-relatively from the volume root through every ancestor to scratch_base.
    - Inspects each directory handle with FileAttributeTagInfo:
      - If FILE_ATTRIBUTE_REPARSE_POINT is present at any level, returns ("reparse_point", None).
      - If any component is not a directory or cannot be opened, returns ("error", None) or ("absent", None).
    - Opens session directory handle pinned to its kernel File Control Block:
      - If it is a reparse point (junction/symlink), returns ("reparse_point", None).
      - If it is an ordinary directory, returns ("verified", h_session).
      - If it does not exist, returns ("absent", None).
    - Zero TOCTOU between verification and caller cleanup because h_session remains open.
    - Status values: 'verified', 'absent', 'reparse_point', 'error'.
    """
    if sys.platform != "win32" or kernel32 is None:
        p = Path(os.path.abspath(str(scratch_base))) / session_id
        if not p.exists():
            return ("absent", None)
        curr = Path(p.parts[0])
        for part in p.parts[1:]:
            curr = curr / part
            if curr.is_symlink():
                return ("reparse_point", None)
        if not p.is_dir():
            return ("error", None)
        return ("verified", None)

    target_path = Path(os.path.abspath(str(scratch_base))) / session_id
    parts = list(target_path.parts)
    if not parts:
        return ("error", None)

    # 1. Open volume root with reparse point protection
    h_root = kernel32.CreateFileW(
        parts[0],
        FILE_READ_ATTRIBUTES | SYNCHRONIZE,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None,
        OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if is_invalid_handle(h_root):
        return ("error", None)

    fati = FILE_ATTRIBUTE_TAG_INFO()
    if not kernel32.GetFileInformationByHandleEx(
        wintypes.HANDLE(h_root),
        FileAttributeTagInfo,
        ctypes.byref(fati),
        ctypes.sizeof(fati),
    ):
        kernel32.CloseHandle(wintypes.HANDLE(h_root))
        return ("error", None)

    if fati.FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
        kernel32.CloseHandle(wintypes.HANDLE(h_root))
        return ("reparse_point", None)

    if not (fati.FileAttributes & FILE_ATTRIBUTE_DIRECTORY):
        kernel32.CloseHandle(wintypes.HANDLE(h_root))
        return ("error", None)

    # 2. Step down every intermediate ancestor handle-relatively
    h_curr = h_root
    for part in parts[1:-1]:
        try:
            h_next = open_relative_child_handle(
                h_curr,
                part,
                desired_access=FILE_READ_ATTRIBUTES | SYNCHRONIZE,
            )
        except NativeApiError as exc:
            kernel32.CloseHandle(wintypes.HANDLE(h_curr))
            err_str = str(exc).lower()
            if "0xc0000034" in err_str or "0xc000003a" in err_str:
                return ("absent", None)
            return ("error", None)
        except Exception:
            kernel32.CloseHandle(wintypes.HANDLE(h_curr))
            return ("error", None)

        kernel32.CloseHandle(wintypes.HANDLE(h_curr))

        fati = FILE_ATTRIBUTE_TAG_INFO()
        if not kernel32.GetFileInformationByHandleEx(
            wintypes.HANDLE(h_next),
            FileAttributeTagInfo,
            ctypes.byref(fati),
            ctypes.sizeof(fati),
        ):
            kernel32.CloseHandle(wintypes.HANDLE(h_next))
            return ("error", None)

        if fati.FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
            kernel32.CloseHandle(wintypes.HANDLE(h_next))
            return ("reparse_point", None)

        if not (fati.FileAttributes & FILE_ATTRIBUTE_DIRECTORY):
            kernel32.CloseHandle(wintypes.HANDLE(h_next))
            return ("error", None)

        h_curr = h_next

    # 3. Open session directory child handle relative to parent scratch_base
    try:
        h_session = open_relative_child_handle(
            h_curr,
            session_id,
            desired_access=desired_access,
        )
    except NativeApiError as exc:
        err_str = str(exc).lower()
        if "0xc0000034" in err_str or "0xc000003a" in err_str:
            return ("absent", None)
        return ("error", None)
    except Exception:
        return ("error", None)
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(h_curr))

    fati = FILE_ATTRIBUTE_TAG_INFO()
    if not kernel32.GetFileInformationByHandleEx(
        wintypes.HANDLE(h_session),
        FileAttributeTagInfo,
        ctypes.byref(fati),
        ctypes.sizeof(fati),
    ):
        kernel32.CloseHandle(wintypes.HANDLE(h_session))
        return ("error", None)

    if fati.FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
        kernel32.CloseHandle(wintypes.HANDLE(h_session))
        return ("reparse_point", None)

    if not (fati.FileAttributes & FILE_ATTRIBUTE_DIRECTORY):
        kernel32.CloseHandle(wintypes.HANDLE(h_session))
        return ("error", None)

    return ("verified", h_session)


class SessionRegistry:
    """Manages permanent session lock tombstones and durable write-ahead metadata transitions."""

    def __init__(
        self,
        base_dir: Path | str | None = None,
        scratch_base: Path | str | None = None,
    ) -> None:
        if base_dir is None:
            self._root = Path(os.path.abspath(str(Path.home() / ".anaya" / "sandbox" / "sessions")))
        else:
            self._root = Path(os.path.abspath(str(base_dir)))

        if scratch_base is None:
            self._scratch_base = Path(os.path.abspath(str(self._root.parent / "scratch")))
        else:
            self._scratch_base = Path(os.path.abspath(str(scratch_base)))

        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        """The root directory housing session lock tombstones and metadata files."""
        return self._root

    @property
    def scratch_base(self) -> Path:
        """The base scratch directory housing per-session scratch spaces."""
        return self._scratch_base

    def _lock_path(self, session_id: str) -> Path:
        return self._root / f"{session_id}.lock"

    def _metadata_path(self, session_id: str) -> Path:
        return self._root / f"{session_id}.json"

    def _tmp_metadata_path(self, session_id: str) -> Path:
        return self._root / f"{session_id}.json.tmp"

    def acquire_session_lock(self, session_id: str, create_new: bool = True) -> int:
        """Acquire an exclusive kernel lock on the session tombstone file.

        Uses CreateFileW with dwShareMode = 0.
        - If create_new=True, uses CREATE_NEW (fails if collision occurs).
        - If create_new=False, uses OPEN_EXISTING.
        Returns the native file handle int. Caller must hold this handle for session lifetime.
        """
        if sys.platform != "win32" or kernel32 is None:
            raise SessionRegistryError("SessionRegistry requires Windows.")

        validated_id = validate_session_id(session_id)
        lock_path = self._lock_path(validated_id)

        disposition = CREATE_NEW if create_new else OPEN_EXISTING
        h_file = kernel32.CreateFileW(
            str(lock_path),
            GENERIC_READ | GENERIC_WRITE,
            0,  # dwShareMode = 0: Exclusive access
            None,
            disposition,
            FILE_ATTRIBUTE_NORMAL,
            None,
        )

        if is_invalid_handle(h_file):
            err = ctypes.get_last_error()
            if err == ERROR_SHARING_VIOLATION:
                raise SessionActiveError(f"Session '{validated_id}' is actively locked by another process.")
            if err == ERROR_FILE_EXISTS:
                raise SessionLockError(f"Session lock '{validated_id}' already exists (collision).")
            raise SessionLockError(f"Failed to acquire session lock for '{validated_id}' with Win32 error {err}.")

        return int(h_file)

    def write_durable_metadata(self, session_id: str, metadata: dict[str, Any]) -> None:
        """Atomically persist session metadata using write-ahead flushing and MoveFileExW."""
        if sys.platform != "win32" or kernel32 is None:
            raise SessionRegistryError("SessionRegistry requires Windows.")

        validated_id = validate_session_id(session_id)
        validate_metadata_schema(metadata, expected_session_id=validated_id)

        payload = json.dumps(metadata, indent=2).encode("utf-8")
        if len(payload) > MAX_METADATA_BYTES:
            raise SessionMetadataError(f"Serialized metadata payload exceeds {MAX_METADATA_BYTES} bytes.")

        tmp_path = self._tmp_metadata_path(validated_id)
        target_path = self._metadata_path(validated_id)

        # 1. Write payload to temporary file
        h_tmp = kernel32.CreateFileW(
            str(tmp_path),
            GENERIC_WRITE,
            0,  # Exclusive
            None,
            CREATE_ALWAYS,
            FILE_ATTRIBUTE_NORMAL,
            None,
        )
        if is_invalid_handle(h_tmp):
            err = ctypes.get_last_error()
            raise SessionMetadataError(f"Failed to create temporary metadata file for '{validated_id}' with error {err}.")

        try:
            written = wintypes.DWORD()
            ok_write = kernel32.WriteFile(h_tmp, payload, len(payload), ctypes.byref(written), None)
            if not ok_write or written.value != len(payload):
                err = ctypes.get_last_error()
                raise SessionMetadataError(f"WriteFile failed for temporary metadata with error {err}.")

            # 2. Flush file buffers to disk
            if not kernel32.FlushFileBuffers(h_tmp):
                err = ctypes.get_last_error()
                raise SessionMetadataError(f"FlushFileBuffers failed for temporary metadata with error {err}.")
        except Exception:
            kernel32.CloseHandle(h_tmp)
            try:
                kernel32.DeleteFileW(str(tmp_path))
            except Exception:
                pass
            raise
        else:
            kernel32.CloseHandle(h_tmp)

        # 3. Atomically replace destination metadata file on NTFS volume
        MOVEFILE_REPLACE_EXISTING = 0x00000001
        MOVEFILE_WRITE_THROUGH = 0x00000008
        ok_move = kernel32.MoveFileExW(
            str(tmp_path),
            str(target_path),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
        )
        if not ok_move:
            err = ctypes.get_last_error()
            try:
                kernel32.DeleteFileW(str(tmp_path))
            except Exception:
                pass
            raise SessionMetadataError(f"MoveFileExW failed to commit metadata replacement with error {err}.")

    def read_metadata(self, session_id: str) -> dict[str, Any]:
        """Read, deserialize, and strictly validate durable metadata for a session."""
        validated_id = validate_session_id(session_id)
        target_path = self._metadata_path(validated_id)

        if not target_path.exists():
            raise SessionMetadataError(f"No metadata file found for session '{validated_id}'.")

        try:
            stat_info = target_path.stat()
            if stat_info.st_size > MAX_METADATA_BYTES:
                raise SessionMetadataError(
                    f"Metadata file '{target_path.name}' exceeds maximum allowed size ({stat_info.st_size} bytes)."
                )

            raw = target_path.read_text(encoding="utf-8")
            data = json.loads(raw, object_pairs_hook=_reject_duplicate_metadata_keys)
        except (json.JSONDecodeError, ValueError) as exc:
            raise SessionMetadataError(f"Corrupt JSON metadata for session '{validated_id}': {exc}") from exc
        except OSError as exc:
            raise SessionMetadataError(f"Failed to read metadata file '{target_path.name}': {exc}") from exc

        validate_metadata_schema(data, expected_session_id=validated_id)
        return data

    def register_session_intent(
        self,
        session_id: str,
        scratch_path: Path | str | None = None,
        profile_name: str | None = None,
    ) -> int:
        """Phase 1: Acquire persistent exclusive lock and write initial intent metadata.

        Returns the native lock handle int. Supervisor must keep this handle open.
        """
        validated_id = validate_session_id(session_id)
        h_lock = self.acquire_session_lock(validated_id, create_new=True)

        supervisor_pid = os.getpid()
        supervisor_start_time = get_process_creation_time(supervisor_pid)

        metadata = {
            "session_id": validated_id,
            "profile_name": profile_name or f"Anaya.Worker.{validated_id}",
            "supervisor_pid": supervisor_pid,
            "supervisor_start_time": supervisor_start_time,
            "worker_pid": None,
            "worker_start_time": None,
            "status": STATUS_PROFILE_PENDING,
            "scratch_path": str(Path(os.path.abspath(str(scratch_path)))) if scratch_path else str(self._scratch_base / validated_id),
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
        }

        try:
            self.write_durable_metadata(validated_id, metadata)
            return h_lock
        except Exception:
            kernel32.CloseHandle(wintypes.HANDLE(h_lock))
            raise

    def update_session_state(self, session_id: str, updates: dict[str, Any]) -> None:
        """Update and commit modified session metadata while lock is held."""
        validated_id = validate_session_id(session_id)
        current = self.read_metadata(validated_id)
        current.update(updates)
        self.write_durable_metadata(validated_id, current)

    def record_worker_suspended(
        self,
        session_id: str,
        worker_pid: int,
        worker_start_time: int | None = None,
    ) -> None:
        """Record worker process identity and creation FILETIME while worker is created suspended."""
        if worker_start_time is None:
            worker_start_time = get_process_creation_time(worker_pid)

        self.update_session_state(
            session_id,
            {
                "worker_pid": int(worker_pid),
                "worker_start_time": worker_start_time,
                "status": STATUS_WORKER_SUSPENDED,
            },
        )

    def record_worker_resumed(self, session_id: str) -> None:
        """Record worker transition to running after successful ResumeThread."""
        self.update_session_state(
            session_id,
            {
                "status": STATUS_WORKER_RUNNING,
            },
        )

    def record_worker_launch(
        self,
        session_id: str,
        worker_pid: int,
        worker_start_time: int | None = None,
    ) -> None:
        """Record worker process identity and creation FILETIME before resuming worker."""
        if worker_start_time is None:
            worker_start_time = get_process_creation_time(worker_pid)

        self.update_session_state(
            session_id,
            {
                "worker_pid": int(worker_pid),
                "worker_start_time": worker_start_time,
                "status": STATUS_WORKER_RUNNING,
            },
        )

    def complete_session(self, session_id: str, h_lock: int) -> None:
        """Mark session completed, remove metadata JSON, and close the lock handle.

        Guarantees that the .lock file is left as a permanent 0-byte tombstone.
        """
        if sys.platform != "win32" or kernel32 is None:
            raise SessionRegistryError("SessionRegistry requires Windows.")

        validated_id = validate_session_id(session_id)
        target_path = self._metadata_path(validated_id)

        try:
            if target_path.exists():
                target_path.unlink(missing_ok=True)
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(h_lock))
            # Permanent tombstone invariant: .lock file is NEVER unlinked!

    def reclaim_orphaned_sessions(
        self,
        on_session_reclaimed: Any | None = None,
    ) -> int:
        """Scan and safely reclaim abandoned sessions whose supervisor and worker are dead.

        Invariants:
        1. Attempts exclusive open on lock file. If ERROR_SHARING_VIOLATION, session is active; skip.
        2. Validates worker PID and creation time. If worker is ALIVE or UNKNOWN, skips immediately.
        3. Validates supervisor PID and creation time. If supervisor is ALIVE or UNKNOWN, skips immediately.
        4. Both processes must be confirmed ProcessLiveness.DEAD before cleanup can proceed.
        5. Derives trusted scratch path and profile name from configured roots; never trusts metadata paths.
        6. Traverses and cleans scratch directory strictly handle-relatively; zero path-based deletion.
        7. Deletes AppContainer profile; strictly accepts only HRESULT 0 (S_OK).
        8. The .lock tombstone is NEVER deleted.
        9. Optional on_session_reclaimed callback receives only the session ID string (no filesystem path).
        Returns the count of successfully reclaimed sessions.
        """
        if sys.platform != "win32" or kernel32 is None:
            return 0

        reclaimed_count = 0

        # Scan for existing metadata JSON files
        for json_file in list(self._root.glob("*.json")):
            if json_file.name.endswith(".tmp") or json_file.name.endswith(".corrupt"):
                continue

            session_id = json_file.stem
            try:
                validated_id = validate_session_id(session_id)
            except InvalidSessionIdError:
                continue

            # Attempt to acquire exclusive lock without creating new file
            try:
                h_lock = self.acquire_session_lock(validated_id, create_new=False)
            except SessionActiveError:
                # Active supervisor is holding lock; skip
                continue
            except SessionLockError:
                # Lock file missing or inaccessible; skip
                continue

            try:
                # Re-read metadata under lock
                try:
                    meta = self.read_metadata(validated_id)
                except SessionMetadataError:
                    # Corrupt or invalid metadata: quarantine JSON file
                    corrupt_path = self._root / f"{validated_id}.json.corrupt"
                    try:
                        json_file.rename(corrupt_path)
                    except Exception:
                        pass
                    continue

                # Conservative process liveness verification (SEC-04)
                worker_pid = meta.get("worker_pid")
                worker_time = meta.get("worker_start_time")
                if worker_pid is not None:
                    w_liveness = check_process_liveness(worker_pid, worker_time)
                    if w_liveness != ProcessLiveness.DEAD:
                        # Worker is confirmed ALIVE or UNKNOWN; do NOT reclaim
                        continue

                supervisor_pid = meta.get("supervisor_pid")
                supervisor_time = meta.get("supervisor_start_time")
                if supervisor_pid is not None:
                    s_liveness = check_process_liveness(supervisor_pid, supervisor_time)
                    if s_liveness != ProcessLiveness.DEAD:
                        # Supervisor is confirmed ALIVE or UNKNOWN; do NOT reclaim
                        continue

                # Both processes are confirmed DEAD.
                # Validate trusted scratch path and profile name (SEC-03 & Reparse-Point Escape Protection)
                trusted_scratch_path = self._scratch_base / validated_id
                trusted_profile_name = f"Anaya.Worker.{validated_id}"

                recorded_scratch = meta.get("scratch_path")
                if recorded_scratch is not None:
                    norm_recorded = os.path.normpath(str(recorded_scratch))
                    norm_trusted = os.path.normpath(str(trusted_scratch_path))
                    if norm_recorded != norm_trusted:
                        logger.error(
                            "Tampered scratch_path in session '%s': recorded '%s', expected '%s'. Quarantining.",
                            validated_id,
                            recorded_scratch,
                            norm_trusted,
                        )
                        corrupt_path = self._root / f"{validated_id}.json.corrupt"
                        try:
                            json_file.rename(corrupt_path)
                        except Exception:
                            pass
                        continue

                recorded_profile = meta.get("profile_name")
                if recorded_profile != trusted_profile_name:
                    logger.error(
                        "Tampered profile_name in session '%s': recorded '%s', expected '%s'. Quarantining.",
                        validated_id,
                        recorded_profile,
                        trusted_profile_name,
                    )
                    corrupt_path = self._root / f"{validated_id}.json.corrupt"
                    try:
                        json_file.rename(corrupt_path)
                    except Exception:
                        pass
                    continue

                # Handle-based ancestor and session directory verification (Section 3)
                scratch_status, h_session = open_verified_session_scratch_handle(
                    self._scratch_base,
                    validated_id,
                )

                if scratch_status == "reparse_point":
                    logger.error(
                        "Reparse point (junction or symlink) detected in scratch path hierarchy for session '%s'. "
                        "Refusing cleanup and quarantining session.",
                        validated_id,
                    )
                    corrupt_path = self._root / f"{validated_id}.json.corrupt"
                    try:
                        json_file.rename(corrupt_path)
                    except Exception:
                        pass
                    continue
                elif scratch_status == "error":
                    logger.error(
                        "Scratch path verification error for session '%s'. Retaining metadata.",
                        validated_id,
                    )
                    continue

                # Execute scratch cleanup strictly through open handle (zero path-based cleanup)
                scratch_ok = False
                if scratch_status == "absent":
                    # Scratch directory is already absent
                    scratch_ok = True
                elif scratch_status == "verified":
                    # Scratch directory exists and is verified ordinary directory
                    h_session_closed = False
                    try:
                        if h_session is not None:
                            clean_directory_handle_relative(h_session)
                            # delete_file_by_handle assumes handle ownership and closes h_session in its finally block
                            h_session_closed = True
                            delete_file_by_handle(h_session)

                        # Confirm absence without invoking any path-based cleanup
                        if not trusted_scratch_path.exists():
                            scratch_ok = True
                        else:
                            logger.error(
                                "Scratch directory '%s' still exists after handle cleanup for session '%s'.",
                                trusted_scratch_path,
                                validated_id,
                            )
                            scratch_ok = False
                    except Exception as exc:
                        logger.error(
                            "Scratch handle cleanup failed for session '%s': %s",
                            validated_id,
                            exc,
                        )
                        scratch_ok = False
                    finally:
                        if not h_session_closed and h_session is not None:
                            kernel32.CloseHandle(wintypes.HANDLE(h_session))

                # Profile deletion: strictly accept ONLY HRESULT 0 (S_OK); nonzero fails closed
                profile_ok = False
                if sys.platform == "win32":
                    if userenv is not None:
                        try:
                            hr = userenv.DeleteAppContainerProfile(trusted_profile_name)
                            # Accept ONLY S_OK (0). Nonzero HRESULTs (including 0x80070490, E_INVALIDARG,
                            # E_ACCESSDENIED) must fail closed unless authoritative documentation establishes otherwise.
                            if hr == 0:
                                profile_ok = True
                            else:
                                logger.error(
                                    "DeleteAppContainerProfile for session '%s' returned nonzero failure HRESULT %#010x.",
                                    validated_id,
                                    hr & 0xFFFFFFFF,
                                )
                        except Exception as exc:
                            logger.error(
                                "DeleteAppContainerProfile for session '%s' raised exception: %s",
                                validated_id,
                                exc,
                            )
                    else:
                        logger.error(
                            "userenv DLL is unavailable on Windows; cannot verify profile deletion for session '%s'.",
                            validated_id,
                        )
                        profile_ok = False
                else:
                    profile_ok = True

                # Do not delete session metadata or increment reclaimed count unless
                # both scratch cleanup and profile cleanup are confirmed successful.
                if scratch_ok and profile_ok:
                    json_file.unlink(missing_ok=True)
                    reclaimed_count += 1
                    if on_session_reclaimed is not None:
                        try:
                            on_session_reclaimed(validated_id)
                        except Exception as cb_exc:
                            logger.warning(
                                "on_session_reclaimed notification failed for session '%s': %s",
                                validated_id,
                                cb_exc,
                            )
                else:
                    logger.warning(
                        "Incomplete cleanup for session '%s' (scratch_ok=%s, profile_ok=%s). "
                        "Retaining metadata for subsequent retry.",
                        validated_id,
                        scratch_ok,
                        profile_ok,
                    )
            finally:
                kernel32.CloseHandle(wintypes.HANDLE(h_lock))
                # Lock tombstone preserved permanently

        # Clean abandoned temporary metadata files
        for tmp_file in list(self._root.glob("*.json.tmp")):
            try:
                tmp_file.unlink(missing_ok=True)
            except Exception:
                pass

        return reclaimed_count
