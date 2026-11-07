"""Tests for Crash-Safe Session Registry, Locking, Schema Validation, and Orphan Reclamation.

Validates:
- Exclusive session locking via dwShareMode=0 (blocking concurrent access with ERROR_SHARING_VIOLATION).
- Permanent lock tombstone retention preventing lock recreate races.
- Strict schema validation, duplicate JSON key rejection, and size limit enforcement.
- Conservative process liveness verification (ALIVE, DEAD, UNKNOWN) preventing premature reclamation.
- Untrusted metadata path and profile validation with quarantine of tampered descriptors.
- Write-ahead metadata transitions with FlushFileBuffers and atomic MoveFileExW replacement.
- Safe orphan reclamation executing cleanup callbacks strictly on trusted derived paths.
- Non-reclamation of actively locked or running worker sessions.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any
import uuid
import pytest

IS_WINDOWS = sys.platform == "win32"
pytestmark = pytest.mark.skipif(not IS_WINDOWS, reason="Session registry locking requires Windows.")

if IS_WINDOWS:
    from app.skills.sandbox.session_registry import (
        ALLOWED_METADATA_FIELDS,
        MAX_METADATA_BYTES,
        STATUS_COMPLETED,
        STATUS_PROFILE_CREATED,
        STATUS_PROFILE_PENDING,
        STATUS_SCRATCH_READY,
        STATUS_WORKER_RUNNING,
        ProcessLiveness,
        SessionActiveError,
        SessionLockError,
        SessionMetadataError,
        SessionRegistry,
        check_process_liveness,
        get_process_creation_time,
        is_process_alive,
        validate_metadata_schema,
        verify_ordinary_directory_not_reparse,
    )
    from app.skills.sandbox.win32_acl_types import kernel32


# ---------------------------------------------------------------------------
# 1. Process Validation & Creation FILETIME & Liveness (SEC-04)
# ---------------------------------------------------------------------------

class TestProcessValidationAndLiveness:
    def test_current_process_creation_time(self) -> None:
        pid = os.getpid()
        ctime = get_process_creation_time(pid)
        assert ctime is not None
        assert ctime > 0

    def test_is_process_alive_current_process(self) -> None:
        pid = os.getpid()
        ctime = get_process_creation_time(pid)
        assert ctime is not None
        assert is_process_alive(pid, ctime) is True

    def test_is_process_alive_wrong_creation_time_fails(self) -> None:
        pid = os.getpid()
        ctime = get_process_creation_time(pid)
        assert ctime is not None
        # Passing an altered creation time simulates PID recycling
        assert is_process_alive(pid, ctime + 1000000) is False

    def test_is_process_alive_nonexistent_pid(self) -> None:
        assert is_process_alive(4000000, None) is False

    def test_liveness_alive_for_running_process(self) -> None:
        pid = os.getpid()
        ctime = get_process_creation_time(pid)
        assert ctime is not None
        assert check_process_liveness(pid, ctime) == ProcessLiveness.ALIVE

    def test_liveness_dead_for_nonexistent_pid(self) -> None:
        # High PID that does not exist
        assert check_process_liveness(4000000, 12345) == ProcessLiveness.DEAD

    def test_liveness_dead_for_recycled_pid(self) -> None:
        pid = os.getpid()
        # Non-matching creation timestamp
        assert check_process_liveness(pid, 1) == ProcessLiveness.DEAD

    def test_liveness_unknown_on_access_denied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _mock_open_process(desired_access: int, inherit_handle: bool, process_id: int) -> int:
            kernel32.SetLastError(5)  # ERROR_ACCESS_DENIED
            return 0

        monkeypatch.setattr(kernel32, "OpenProcess", _mock_open_process)
        assert check_process_liveness(1234, 5678) == ProcessLiveness.UNKNOWN


# ---------------------------------------------------------------------------
# 2. Exclusive Locking & Permanent Tombstones
# ---------------------------------------------------------------------------

class TestSessionLocking:
    def test_acquire_and_release_lock(self, tmp_path: Path) -> None:
        registry = SessionRegistry(tmp_path)
        session_id = str(uuid.uuid4())

        h_lock = registry.acquire_session_lock(session_id, create_new=True)
        assert h_lock > 0

        lock_file = tmp_path / f"{session_id}.lock"
        assert lock_file.is_file()

        # Concurrent acquisition attempt must fail with SessionActiveError
        with pytest.raises(SessionActiveError):
            registry.acquire_session_lock(session_id, create_new=False)

        # Release the lock
        kernel32.CloseHandle(wintypes.HANDLE(h_lock))

        # Re-acquisition must succeed now that it's closed
        h_lock2 = registry.acquire_session_lock(session_id, create_new=False)
        assert h_lock2 > 0
        kernel32.CloseHandle(wintypes.HANDLE(h_lock2))

        # Permanent tombstone: lock file MUST still exist!
        assert lock_file.is_file()

    def test_acquire_session_lock_missing_file_raises(self, tmp_path: Path) -> None:
        registry = SessionRegistry(tmp_path)
        session_id = str(uuid.uuid4())

        # When create_new=False and lock doesn't exist, must raise SessionLockError
        with pytest.raises(SessionLockError):
            registry.acquire_session_lock(session_id, create_new=False)


# ---------------------------------------------------------------------------
# 3. Durable Metadata Transitions & Strict Schema Validation (SEC-05)
# ---------------------------------------------------------------------------

class TestSessionMetadata:
    def test_metadata_write_and_read(self, tmp_path: Path) -> None:
        registry = SessionRegistry(tmp_path)
        session_id = str(uuid.uuid4())

        h_lock = registry.register_session_intent(session_id)
        try:
            # Verify read
            read_back = registry.read_metadata(session_id)
            assert read_back["session_id"] == session_id
            assert read_back["status"] == STATUS_PROFILE_PENDING
            assert read_back["profile_name"] == f"Anaya.Worker.{session_id}"

            # Transition status
            registry.update_session_state(session_id, {"status": STATUS_SCRATCH_READY})
            updated = registry.read_metadata(session_id)
            assert updated["status"] == STATUS_SCRATCH_READY

            # Record worker launch
            registry.record_worker_launch(session_id, worker_pid=1234, worker_start_time=5678)
            launched = registry.read_metadata(session_id)
            assert launched["status"] == STATUS_WORKER_RUNNING
            assert launched["worker_pid"] == 1234
            assert launched["worker_start_time"] == 5678

            # Complete session (unlinks json, closes lock, retains tombstone)
            registry.complete_session(session_id, h_lock)
            assert not (tmp_path / f"{session_id}.json").exists()
            assert (tmp_path / f"{session_id}.lock").exists()
        except Exception:
            kernel32.CloseHandle(wintypes.HANDLE(h_lock))
            raise

    def test_read_nonexistent_metadata_raises(self, tmp_path: Path) -> None:
        registry = SessionRegistry(tmp_path)
        session_id = str(uuid.uuid4())
        with pytest.raises(SessionMetadataError):
            registry.read_metadata(session_id)

    def test_corrupt_metadata_raises(self, tmp_path: Path) -> None:
        registry = SessionRegistry(tmp_path)
        session_id = str(uuid.uuid4())
        json_file = tmp_path / f"{session_id}.json"
        json_file.write_text("{invalid_json: true", encoding="utf-8")

        with pytest.raises(SessionMetadataError):
            registry.read_metadata(session_id)

    def test_reject_duplicate_json_keys(self, tmp_path: Path) -> None:
        registry = SessionRegistry(tmp_path)
        session_id = str(uuid.uuid4())
        json_file = tmp_path / f"{session_id}.json"

        # Raw payload with duplicate keys
        duplicate_payload = f"""{{
            "session_id": "{session_id}",
            "profile_name": "Anaya.Worker.{session_id}",
            "status": "{STATUS_PROFILE_PENDING}",
            "session_id": "{session_id}"
        }}"""
        json_file.write_text(duplicate_payload, encoding="utf-8")

        with pytest.raises(SessionMetadataError, match="Duplicate metadata JSON key detected"):
            registry.read_metadata(session_id)

    def test_reject_unexpected_fields(self, tmp_path: Path) -> None:
        session_id = str(uuid.uuid4())
        invalid_meta = {
            "session_id": session_id,
            "profile_name": f"Anaya.Worker.{session_id}",
            "status": STATUS_PROFILE_PENDING,
            "malicious_injected_field": "exploit",
        }
        with pytest.raises(SessionMetadataError, match="Unexpected metadata field"):
            validate_metadata_schema(invalid_meta, expected_session_id=session_id)

    def test_reject_invalid_pid_range(self, tmp_path: Path) -> None:
        session_id = str(uuid.uuid4())
        invalid_meta = {
            "session_id": session_id,
            "profile_name": f"Anaya.Worker.{session_id}",
            "status": STATUS_PROFILE_PENDING,
            "worker_pid": -10,
        }
        with pytest.raises(SessionMetadataError, match="Invalid worker_pid"):
            validate_metadata_schema(invalid_meta, expected_session_id=session_id)

    def test_reject_invalid_status(self, tmp_path: Path) -> None:
        session_id = str(uuid.uuid4())
        invalid_meta = {
            "session_id": session_id,
            "profile_name": f"Anaya.Worker.{session_id}",
            "status": "NONEXISTENT_STATUS",
        }
        with pytest.raises(SessionMetadataError, match="Invalid metadata status"):
            validate_metadata_schema(invalid_meta, expected_session_id=session_id)

    def test_reject_invalid_timestamp(self, tmp_path: Path) -> None:
        session_id = str(uuid.uuid4())
        invalid_meta = {
            "session_id": session_id,
            "profile_name": f"Anaya.Worker.{session_id}",
            "status": STATUS_PROFILE_PENDING,
            "created_at_utc": "not-a-timestamp",
        }
        with pytest.raises(SessionMetadataError, match="Invalid created_at_utc"):
            validate_metadata_schema(invalid_meta, expected_session_id=session_id)

    def test_reject_oversized_metadata(self, tmp_path: Path) -> None:
        registry = SessionRegistry(tmp_path)
        session_id = str(uuid.uuid4())
        json_file = tmp_path / f"{session_id}.json"
        # Write > 64 KB of spaces into valid JSON
        padding = " " * (MAX_METADATA_BYTES + 100)
        json_file.write_text(f'{{"session_id": "{session_id}"{padding}}}', encoding="utf-8")

        with pytest.raises(SessionMetadataError, match="exceeds maximum allowed size"):
            registry.read_metadata(session_id)


# ---------------------------------------------------------------------------
# 4. Crash-Safe Orphan Reclamation & Untrusted Path Quarantine (SEC-03, SEC-04)
# ---------------------------------------------------------------------------

class TestSessionReclamation:
    def test_active_session_is_not_reclaimed(self, tmp_path: Path) -> None:
        registry = SessionRegistry(tmp_path)
        session_id = str(uuid.uuid4())

        h_lock = registry.register_session_intent(session_id)
        try:
            reclaimed_ids: list[str] = []
            reclaimed = registry.reclaim_orphaned_sessions(
                on_session_reclaimed=lambda sid: reclaimed_ids.append(sid)
            )
            assert reclaimed == 0
            assert len(reclaimed_ids) == 0
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(h_lock))

    def test_dead_orphan_is_reclaimed(self, tmp_path: Path) -> None:
        sessions_dir = tmp_path / "sessions"
        scratch_base = tmp_path / "scratch"
        registry = SessionRegistry(base_dir=sessions_dir, scratch_base=scratch_base)
        session_id = str(uuid.uuid4())

        trusted_scratch = scratch_base / session_id
        trusted_scratch.mkdir(parents=True, exist_ok=True)
        (trusted_scratch / "child.txt").write_text("worker data", encoding="utf-8")

        h_lock = registry.register_session_intent(session_id, scratch_path=trusted_scratch)
        try:
            meta = registry.read_metadata(session_id)
            meta["supervisor_pid"] = 4000000
            meta["supervisor_start_time"] = 12345
            meta["worker_pid"] = 4000004
            meta["worker_start_time"] = 67890
            registry.write_durable_metadata(session_id, meta)
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(h_lock))

        reclaimed_ids: list[str] = []
        reclaimed = registry.reclaim_orphaned_sessions(
            on_session_reclaimed=lambda sid: reclaimed_ids.append(sid)
        )
        assert reclaimed == 1
        assert reclaimed_ids == [session_id]
        assert not trusted_scratch.exists()

        # Metadata JSON file must be removed
        assert not (sessions_dir / f"{session_id}.json").exists()
        # Permanent .lock tombstone MUST remain
        assert (sessions_dir / f"{session_id}.lock").exists()

    def test_unknown_liveness_blocks_reclamation(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When check_process_liveness returns UNKNOWN, reclamation must NOT proceed."""
        sessions_dir = tmp_path / "sessions"
        scratch_base = tmp_path / "scratch"
        registry = SessionRegistry(base_dir=sessions_dir, scratch_base=scratch_base)
        session_id = str(uuid.uuid4())

        trusted_scratch = scratch_base / session_id
        trusted_scratch.mkdir(parents=True, exist_ok=True)

        h_lock = registry.register_session_intent(session_id, scratch_path=trusted_scratch)
        try:
            meta = registry.read_metadata(session_id)
            meta["supervisor_pid"] = 4000000
            meta["supervisor_start_time"] = 12345
            meta["worker_pid"] = 4000004
            meta["worker_start_time"] = 67890
            registry.write_durable_metadata(session_id, meta)
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(h_lock))

        # Mock check_process_liveness to return UNKNOWN for supervisor
        from app.skills.sandbox import session_registry
        original_check = session_registry.check_process_liveness

        def _mock_check(pid: int, expected_creation_time: int | None) -> ProcessLiveness:
            if pid == 4000000:
                return ProcessLiveness.UNKNOWN
            return original_check(pid, expected_creation_time)

        monkeypatch.setattr(session_registry, "check_process_liveness", _mock_check)

        reclaimed = registry.reclaim_orphaned_sessions()

        # Reclamation was blocked!
        assert reclaimed == 0
        assert (sessions_dir / f"{session_id}.json").exists()
        assert trusted_scratch.exists()

    def test_tampered_scratch_path_is_quarantined_without_cleanup(self, tmp_path: Path) -> None:
        """Metadata containing a scratch path pointing outside scratch root must be quarantined."""
        sessions_dir = tmp_path / "sessions"
        scratch_base = tmp_path / "scratch"
        registry = SessionRegistry(base_dir=sessions_dir, scratch_base=scratch_base)
        session_id = str(uuid.uuid4())

        canary_dir = tmp_path / "system_canary_dir"
        canary_dir.mkdir(parents=True, exist_ok=True)

        h_lock = registry.register_session_intent(session_id)
        try:
            meta = registry.read_metadata(session_id)
            meta["supervisor_pid"] = 4000000
            meta["supervisor_start_time"] = 12345
            meta["worker_pid"] = 4000004
            meta["worker_start_time"] = 67890
            # Tampered scratch path pointing to external directory!
            meta["scratch_path"] = str(canary_dir.resolve())
            # Directly write to disk to bypass write_durable_metadata if necessary or test write
            # Since write_durable_metadata validates schema (including scratch_path is string),
            # write_durable_metadata allows valid schema strings.
            registry.write_durable_metadata(session_id, meta)
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(h_lock))

        reclaimed = registry.reclaim_orphaned_sessions()

        assert reclaimed == 0
        # Canary directory was untouched
        assert canary_dir.exists()
        # Metadata was quarantined to .json.corrupt
        assert (sessions_dir / f"{session_id}.json.corrupt").exists()
        assert not (sessions_dir / f"{session_id}.json").exists()

    def test_tampered_profile_name_is_quarantined_without_deletion(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Metadata with a foreign profile name must be quarantined without deleting the profile."""
        sessions_dir = tmp_path / "sessions"
        scratch_base = tmp_path / "scratch"
        registry = SessionRegistry(base_dir=sessions_dir, scratch_base=scratch_base)
        session_id = str(uuid.uuid4())

        h_lock = registry.register_session_intent(session_id)
        try:
            # Manually bypass write_durable_metadata schema check to write tampered JSON directly
            raw_meta = {
                "session_id": session_id,
                "profile_name": "Anaya.Worker.foreign_system_profile",
                "supervisor_pid": 4000000,
                "supervisor_start_time": 12345,
                "worker_pid": 4000004,
                "worker_start_time": 67890,
                "status": STATUS_PROFILE_PENDING,
                "scratch_path": str((scratch_base / session_id).resolve()),
                "created_at_utc": "2026-11-07T12:00:00+00:00",
            }
            (sessions_dir / f"{session_id}.json").write_text(json.dumps(raw_meta), encoding="utf-8")
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(h_lock))

        profile_deleted = False

        class _MockUserEnv:
            def DeleteAppContainerProfile(self, name: str) -> int:
                nonlocal profile_deleted
                profile_deleted = True
                return 0

        from app.skills.sandbox import session_registry
        monkeypatch.setattr(session_registry, "userenv", _MockUserEnv())

        reclaimed = registry.reclaim_orphaned_sessions()

        assert reclaimed == 0
        assert not profile_deleted
        # Metadata was quarantined to .json.corrupt
        assert (sessions_dir / f"{session_id}.json.corrupt").exists()
        assert not (sessions_dir / f"{session_id}.json").exists()


# ---------------------------------------------------------------------------
# 5. Scratch-Root Reparse-Point Escape Protection (Section 1)
# ---------------------------------------------------------------------------

class TestScratchReparsePointEscape:
    def test_junction_scratch_dir_refuses_cleanup_and_quarantines(
        self,
        tmp_path: Path,
    ) -> None:
        """When session scratch is replaced by a junction to an external directory:
        1. Cleanup must be refused.
        2. External canary files MUST remain untouched.
        3. Cleanup callback is NEVER invoked with the external target path.
        4. Session metadata is quarantined.
        """
        sessions_dir = tmp_path / "sessions"
        scratch_base = tmp_path / "scratch"
        scratch_base.mkdir(parents=True, exist_ok=True)
        registry = SessionRegistry(base_dir=sessions_dir, scratch_base=scratch_base)
        session_id = str(uuid.uuid4())

        external_target = tmp_path / "critical_system_data"
        external_target.mkdir(parents=True, exist_ok=True)
        canary = external_target / "do_not_delete_me.txt"
        canary.write_text("critical external canary content", encoding="utf-8")

        # Replace session scratch path with an NTFS junction pointing to external target
        junction_scratch = scratch_base / session_id
        cmd_res = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction_scratch), str(external_target)],
            capture_output=True,
            text=True,
        )
        if cmd_res.returncode != 0:
            pytest.skip("Creating NTFS junction requires appropriate filesystem privileges.")

        assert junction_scratch.is_dir()
        assert (junction_scratch / "do_not_delete_me.txt").is_file()

        h_lock = registry.register_session_intent(session_id, scratch_path=junction_scratch)
        try:
            meta = registry.read_metadata(session_id)
            meta["supervisor_pid"] = 4000000
            meta["supervisor_start_time"] = 12345
            meta["worker_pid"] = 4000004
            meta["worker_start_time"] = 67890
            registry.write_durable_metadata(session_id, meta)
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(h_lock))

        reclaimed_ids: list[str] = []
        reclaimed = registry.reclaim_orphaned_sessions(
            on_session_reclaimed=lambda sid: reclaimed_ids.append(sid)
        )

        # 1. Cleanup refused
        assert reclaimed == 0
        # 2. Notification callback NEVER called
        assert len(reclaimed_ids) == 0
        # 3. External target and canary MUST remain completely untouched!
        assert external_target.is_dir()
        assert canary.is_file()
        assert canary.read_text(encoding="utf-8") == "critical external canary content"
        # 4. Session is safely quarantined
        assert (sessions_dir / f"{session_id}.json.corrupt").exists()
        assert not (sessions_dir / f"{session_id}.json").exists()

    def test_symlink_scratch_dir_refuses_cleanup_and_quarantines(
        self,
        tmp_path: Path,
    ) -> None:
        """When session scratch is replaced by a symbolic link, cleanup must be refused."""
        sessions_dir = tmp_path / "sessions"
        scratch_base = tmp_path / "scratch"
        scratch_base.mkdir(parents=True, exist_ok=True)
        registry = SessionRegistry(base_dir=sessions_dir, scratch_base=scratch_base)
        session_id = str(uuid.uuid4())

        external_target = tmp_path / "external_symlink_target"
        external_target.mkdir(parents=True, exist_ok=True)
        canary = external_target / "symlink_canary.txt"
        canary.write_text("symlink target preserved", encoding="utf-8")

        symlink_scratch = scratch_base / session_id
        try:
            os.symlink(external_target, symlink_scratch, target_is_directory=True)
        except OSError:
            pytest.skip("Creating directory symlinks requires SeCreateSymbolicLinkPrivilege / Developer Mode.")

        h_lock = registry.register_session_intent(session_id, scratch_path=symlink_scratch)
        try:
            meta = registry.read_metadata(session_id)
            meta["supervisor_pid"] = 4000000
            meta["supervisor_start_time"] = 12345
            meta["worker_pid"] = 4000004
            meta["worker_start_time"] = 67890
            registry.write_durable_metadata(session_id, meta)
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(h_lock))

        reclaimed_ids: list[str] = []
        reclaimed = registry.reclaim_orphaned_sessions(
            on_session_reclaimed=lambda sid: reclaimed_ids.append(sid)
        )

        assert reclaimed == 0
        assert len(reclaimed_ids) == 0
        assert external_target.is_dir()
        assert canary.is_file()
        assert canary.read_text(encoding="utf-8") == "symlink target preserved"
        assert (sessions_dir / f"{session_id}.json.corrupt").exists()

    def test_scratch_base_itself_is_junction_refuses_cleanup_and_quarantines(
        self,
        tmp_path: Path,
    ) -> None:
        """When scratch_base itself is replaced by an NTFS junction to an external directory:
        1. Handle traversal detects reparse point on scratch_base.
        2. Cleanup must be refused and session quarantined.
        3. External canary files remain intact.
        """
        sessions_dir = tmp_path / "sessions"
        scratch_base = tmp_path / "scratch_junc"
        external_target = tmp_path / "canary_store"
        external_target.mkdir(parents=True, exist_ok=True)
        canary = external_target / "secret.txt"
        canary.write_text("secret untouched", encoding="utf-8")

        cmd_res = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(scratch_base), str(external_target)],
            capture_output=True,
            text=True,
        )
        if cmd_res.returncode != 0:
            pytest.skip("Creating NTFS junction requires appropriate filesystem privileges.")

        registry = SessionRegistry(base_dir=sessions_dir, scratch_base=scratch_base)
        session_id = str(uuid.uuid4())

        h_lock = registry.register_session_intent(session_id, scratch_path=scratch_base / session_id)
        try:
            meta = registry.read_metadata(session_id)
            meta["supervisor_pid"] = 4000000
            meta["supervisor_start_time"] = 12345
            meta["worker_pid"] = 4000004
            meta["worker_start_time"] = 67890
            registry.write_durable_metadata(session_id, meta)
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(h_lock))

        reclaimed_ids: list[str] = []
        reclaimed = registry.reclaim_orphaned_sessions(
            on_session_reclaimed=lambda sid: reclaimed_ids.append(sid)
        )

        assert reclaimed == 0
        assert len(reclaimed_ids) == 0
        assert external_target.is_dir()
        assert canary.is_file()
        assert canary.read_text(encoding="utf-8") == "secret untouched"
        assert (sessions_dir / f"{session_id}.json.corrupt").exists()

    def test_ancestor_directory_is_junction_refuses_cleanup_and_quarantines(
        self,
        tmp_path: Path,
    ) -> None:
        """When an ancestor directory above scratch_base is an NTFS junction:
        1. Handle traversal detects reparse point at the ancestor component.
        2. Cleanup must be refused and session quarantined.
        3. External canary files remain intact.
        """
        sessions_dir = tmp_path / "sessions"
        anc_dir = tmp_path / "anc_junc"
        external_target = tmp_path / "external_anc"
        external_target.mkdir(parents=True, exist_ok=True)
        (external_target / "scratch").mkdir(parents=True, exist_ok=True)
        canary = external_target / "anc_canary.txt"
        canary.write_text("anc canary intact", encoding="utf-8")

        cmd_res = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(anc_dir), str(external_target)],
            capture_output=True,
            text=True,
        )
        if cmd_res.returncode != 0:
            pytest.skip("Creating NTFS junction requires appropriate filesystem privileges.")

        scratch_base = anc_dir / "scratch"
        registry = SessionRegistry(base_dir=sessions_dir, scratch_base=scratch_base)
        session_id = str(uuid.uuid4())

        h_lock = registry.register_session_intent(session_id, scratch_path=scratch_base / session_id)
        try:
            meta = registry.read_metadata(session_id)
            meta["supervisor_pid"] = 4000000
            meta["supervisor_start_time"] = 12345
            meta["worker_pid"] = 4000004
            meta["worker_start_time"] = 67890
            registry.write_durable_metadata(session_id, meta)
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(h_lock))

        reclaimed_ids: list[str] = []
        reclaimed = registry.reclaim_orphaned_sessions(
            on_session_reclaimed=lambda sid: reclaimed_ids.append(sid)
        )

        assert reclaimed == 0
        assert len(reclaimed_ids) == 0
        assert external_target.is_dir()
        assert canary.is_file()
        assert canary.read_text(encoding="utf-8") == "anc canary intact"
        assert (sessions_dir / f"{session_id}.json.corrupt").exists()

    def test_handle_pinned_cleanup_prevents_toctou_path_substitution(
        self,
        tmp_path: Path,
    ) -> None:
        """Verify that handle-relative cleanup operates exclusively on the verified directory handle.

        Demonstrates TOCTOU resistance:
        1. Opens verified handle h_session to scratch_base / session_id.
        2. Simulates an adversary substituting the filesystem path while h_session is held open:
           replaces scratch_base / session_id with an NTFS junction pointing to external_dir.
        3. Cleans strictly via h_session.
        4. Proves the original directory was deleted while external_dir canary remains untouched.
        """
        scratch_base = tmp_path / "scratch"
        scratch_base.mkdir(parents=True, exist_ok=True)
        session_id = str(uuid.uuid4())
        session_dir = scratch_base / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "worker_scratch.tmp").write_text("worker temp file", encoding="utf-8")

        external_dir = tmp_path / "external_canary"
        external_dir.mkdir(parents=True, exist_ok=True)
        canary = external_dir / "protected.txt"
        canary.write_text("protected canary", encoding="utf-8")

        from app.skills.sandbox.session_registry import open_verified_session_scratch_handle
        from app.skills.sandbox.win32_acl import clean_directory_handle_relative, delete_file_by_handle

        status, h_session = open_verified_session_scratch_handle(scratch_base, session_id)
        assert status == "verified"
        assert h_session is not None

        # TOCTOU attack simulation:
        # Move session_dir and replace original path with an NTFS junction to external_dir
        session_dir_moved = scratch_base / f"{session_id}_moved"
        os.rename(session_dir, session_dir_moved)

        cmd_res = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(session_dir), str(external_dir)],
            capture_output=True,
            text=True,
        )
        if cmd_res.returncode != 0:
            kernel32.CloseHandle(wintypes.HANDLE(h_session))
            pytest.skip("Creating NTFS junction requires appropriate filesystem privileges.")

        assert session_dir.is_dir()
        assert (session_dir / "protected.txt").is_file()

        h_closed = False
        try:
            clean_directory_handle_relative(h_session)
            h_closed = True
            delete_file_by_handle(h_session)
        finally:
            if not h_closed:
                kernel32.CloseHandle(wintypes.HANDLE(h_session))

        # Original directory target pinned to h_session was deleted
        assert not session_dir_moved.exists()
        # External target was NEVER traversed; canary survives intact
        assert canary.is_file()
        assert canary.read_text(encoding="utf-8") == "protected canary"


# ---------------------------------------------------------------------------
# 6. AppContainer Profile Deletion Result Handling (Section 2)
# ---------------------------------------------------------------------------

class TestProfileDeletionResultHandling:
    def test_profile_deletion_success_reclaims_session(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When DeleteAppContainerProfile returns S_OK (0), cleanup completes successfully."""
        sessions_dir = tmp_path / "sessions"
        scratch_base = tmp_path / "scratch"
        registry = SessionRegistry(base_dir=sessions_dir, scratch_base=scratch_base)
        session_id = str(uuid.uuid4())

        trusted_scratch = scratch_base / session_id
        trusted_scratch.mkdir(parents=True, exist_ok=True)

        h_lock = registry.register_session_intent(session_id, scratch_path=trusted_scratch)
        try:
            meta = registry.read_metadata(session_id)
            meta["supervisor_pid"] = 4000000
            meta["supervisor_start_time"] = 12345
            meta["worker_pid"] = 4000004
            meta["worker_start_time"] = 67890
            registry.write_durable_metadata(session_id, meta)
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(h_lock))

        class _MockUserEnv:
            def DeleteAppContainerProfile(self, name: str) -> int:
                return 0  # S_OK

        from app.skills.sandbox import session_registry
        monkeypatch.setattr(session_registry, "userenv", _MockUserEnv())

        reclaimed = registry.reclaim_orphaned_sessions()
        assert reclaimed == 1
        assert not (sessions_dir / f"{session_id}.json").exists()
        assert (sessions_dir / f"{session_id}.lock").exists()

    def test_profile_deletion_error_not_found_hresult_fails_closed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When DeleteAppContainerProfile returns HRESULT_FROM_WIN32(ERROR_NOT_FOUND) (0x80070490),
        it must NOT be accepted as clean; it must fail closed and retain metadata.
        """
        sessions_dir = tmp_path / "sessions"
        scratch_base = tmp_path / "scratch"
        registry = SessionRegistry(base_dir=sessions_dir, scratch_base=scratch_base)
        session_id = str(uuid.uuid4())

        trusted_scratch = scratch_base / session_id
        trusted_scratch.mkdir(parents=True, exist_ok=True)

        h_lock = registry.register_session_intent(session_id, scratch_path=trusted_scratch)
        try:
            meta = registry.read_metadata(session_id)
            meta["supervisor_pid"] = 4000000
            meta["supervisor_start_time"] = 12345
            meta["worker_pid"] = 4000004
            meta["worker_start_time"] = 67890
            registry.write_durable_metadata(session_id, meta)
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(h_lock))

        class _MockUserEnv:
            def DeleteAppContainerProfile(self, name: str) -> int:
                return -2147023728  # 0x80070490: HRESULT_FROM_WIN32(ERROR_NOT_FOUND)

        from app.skills.sandbox import session_registry
        monkeypatch.setattr(session_registry, "userenv", _MockUserEnv())

        reclaimed = registry.reclaim_orphaned_sessions()
        assert reclaimed == 0
        assert (sessions_dir / f"{session_id}.json").exists()

    def test_profile_deletion_invalid_arg_fails_closed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When DeleteAppContainerProfile returns E_INVALIDARG (0x80070057),
        cleanup must fail closed and retain metadata.
        """
        sessions_dir = tmp_path / "sessions"
        scratch_base = tmp_path / "scratch"
        registry = SessionRegistry(base_dir=sessions_dir, scratch_base=scratch_base)
        session_id = str(uuid.uuid4())

        trusted_scratch = scratch_base / session_id
        trusted_scratch.mkdir(parents=True, exist_ok=True)

        h_lock = registry.register_session_intent(session_id, scratch_path=trusted_scratch)
        try:
            meta = registry.read_metadata(session_id)
            meta["supervisor_pid"] = 4000000
            meta["supervisor_start_time"] = 12345
            meta["worker_pid"] = 4000004
            meta["worker_start_time"] = 67890
            registry.write_durable_metadata(session_id, meta)
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(h_lock))

        class _MockUserEnv:
            def DeleteAppContainerProfile(self, name: str) -> int:
                return -2147024809  # 0x80070057: E_INVALIDARG

        from app.skills.sandbox import session_registry
        monkeypatch.setattr(session_registry, "userenv", _MockUserEnv())

        reclaimed = registry.reclaim_orphaned_sessions()
        assert reclaimed == 0
        assert (sessions_dir / f"{session_id}.json").exists()

    def test_profile_deletion_access_denied_retains_metadata(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When DeleteAppContainerProfile returns E_ACCESSDENIED (0x80070005),
        cleanup must NOT be considered complete; metadata must be retained.
        """
        sessions_dir = tmp_path / "sessions"
        scratch_base = tmp_path / "scratch"
        registry = SessionRegistry(base_dir=sessions_dir, scratch_base=scratch_base)
        session_id = str(uuid.uuid4())

        trusted_scratch = scratch_base / session_id
        trusted_scratch.mkdir(parents=True, exist_ok=True)

        h_lock = registry.register_session_intent(session_id, scratch_path=trusted_scratch)
        try:
            meta = registry.read_metadata(session_id)
            meta["supervisor_pid"] = 4000000
            meta["supervisor_start_time"] = 12345
            meta["worker_pid"] = 4000004
            meta["worker_start_time"] = 67890
            registry.write_durable_metadata(session_id, meta)
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(h_lock))

        class _MockUserEnv:
            def DeleteAppContainerProfile(self, name: str) -> int:
                return -2147024891  # 0x80070005: E_ACCESSDENIED

        from app.skills.sandbox import session_registry
        monkeypatch.setattr(session_registry, "userenv", _MockUserEnv())

        reclaimed = registry.reclaim_orphaned_sessions()
        assert reclaimed == 0
        # Metadata file MUST survive!
        assert (sessions_dir / f"{session_id}.json").exists()

    def test_profile_deletion_unexpected_hresult_retains_metadata(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When DeleteAppContainerProfile returns an unexpected error HRESULT (e.g. 0x80004005 E_FAIL),
        cleanup must fail and retain metadata.
        """
        sessions_dir = tmp_path / "sessions"
        scratch_base = tmp_path / "scratch"
        registry = SessionRegistry(base_dir=sessions_dir, scratch_base=scratch_base)
        session_id = str(uuid.uuid4())

        trusted_scratch = scratch_base / session_id
        trusted_scratch.mkdir(parents=True, exist_ok=True)

        h_lock = registry.register_session_intent(session_id, scratch_path=trusted_scratch)
        try:
            meta = registry.read_metadata(session_id)
            meta["supervisor_pid"] = 4000000
            meta["supervisor_start_time"] = 12345
            meta["worker_pid"] = 4000004
            meta["worker_start_time"] = 67890
            registry.write_durable_metadata(session_id, meta)
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(h_lock))

        class _MockUserEnv:
            def DeleteAppContainerProfile(self, name: str) -> int:
                return -2147467259  # 0x80004005: E_FAIL

        from app.skills.sandbox import session_registry
        monkeypatch.setattr(session_registry, "userenv", _MockUserEnv())

        reclaimed = registry.reclaim_orphaned_sessions()
        assert reclaimed == 0
        assert (sessions_dir / f"{session_id}.json").exists()

    def test_scratch_cleaned_profile_failed_then_subsequent_retry_succeeds(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify multi-pass idempotent reclamation:
        Pass 1: Scratch directory is cleaned, but profile deletion fails (e.g. profile in use).
                Metadata is retained.
        Pass 2: Profile is no longer in use; profile deletion succeeds.
                Metadata is deleted and session is successfully reclaimed.
        """
        sessions_dir = tmp_path / "sessions"
        scratch_base = tmp_path / "scratch"
        registry = SessionRegistry(base_dir=sessions_dir, scratch_base=scratch_base)
        session_id = str(uuid.uuid4())

        trusted_scratch = scratch_base / session_id
        trusted_scratch.mkdir(parents=True, exist_ok=True)
        (trusted_scratch / "leftover.txt").write_text("temporary scratch data", encoding="utf-8")

        h_lock = registry.register_session_intent(session_id, scratch_path=trusted_scratch)
        try:
            meta = registry.read_metadata(session_id)
            meta["supervisor_pid"] = 4000000
            meta["supervisor_start_time"] = 12345
            meta["worker_pid"] = 4000004
            meta["worker_start_time"] = 67890
            registry.write_durable_metadata(session_id, meta)
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(h_lock))

        profile_accessible = False

        class _MockUserEnv:
            def DeleteAppContainerProfile(self, name: str) -> int:
                if not profile_accessible:
                    return -2147024891  # E_ACCESSDENIED
                return 0  # S_OK

        from app.skills.sandbox import session_registry
        monkeypatch.setattr(session_registry, "userenv", _MockUserEnv())

        # Pass 1: profile fails
        reclaimed_p1 = registry.reclaim_orphaned_sessions()
        assert reclaimed_p1 == 0
        assert not trusted_scratch.exists()  # Scratch was cleaned handle-relatively
        assert (sessions_dir / f"{session_id}.json").exists()  # Metadata preserved for retry!

        # Pass 2: failure condition cleared
        profile_accessible = True
        reclaimed_p2 = registry.reclaim_orphaned_sessions()
        assert reclaimed_p2 == 1
        assert not (sessions_dir / f"{session_id}.json").exists()  # Metadata now removed
        assert (sessions_dir / f"{session_id}.lock").exists()  # Lock tombstone preserved

    def test_profile_deletion_userenv_none_fails_closed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When userenv is None on Windows, profile deletion cannot be performed,
        so cleanup must fail closed and retain metadata.
        """
        sessions_dir = tmp_path / "sessions"
        scratch_base = tmp_path / "scratch"
        registry = SessionRegistry(base_dir=sessions_dir, scratch_base=scratch_base)
        session_id = str(uuid.uuid4())

        trusted_scratch = scratch_base / session_id
        trusted_scratch.mkdir(parents=True, exist_ok=True)

        h_lock = registry.register_session_intent(session_id, scratch_path=trusted_scratch)
        try:
            meta = registry.read_metadata(session_id)
            meta["supervisor_pid"] = 4000000
            meta["supervisor_start_time"] = 12345
            meta["worker_pid"] = 4000004
            meta["worker_start_time"] = 67890
            registry.write_durable_metadata(session_id, meta)
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(h_lock))

        from app.skills.sandbox import session_registry
        monkeypatch.setattr(session_registry, "userenv", None)

        reclaimed = registry.reclaim_orphaned_sessions()
        assert reclaimed == 0
        assert (sessions_dir / f"{session_id}.json").exists()

    def test_profile_deletion_file_not_found_hresult_fails_closed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Unrelated filesystem error 0x80070002 (ERROR_FILE_NOT_FOUND) must NOT be assumed
        to indicate absent profile; it must fail closed and retain metadata.
        """
        sessions_dir = tmp_path / "sessions"
        scratch_base = tmp_path / "scratch"
        registry = SessionRegistry(base_dir=sessions_dir, scratch_base=scratch_base)
        session_id = str(uuid.uuid4())

        trusted_scratch = scratch_base / session_id
        trusted_scratch.mkdir(parents=True, exist_ok=True)

        h_lock = registry.register_session_intent(session_id, scratch_path=trusted_scratch)
        try:
            meta = registry.read_metadata(session_id)
            meta["supervisor_pid"] = 4000000
            meta["supervisor_start_time"] = 12345
            meta["worker_pid"] = 4000004
            meta["worker_start_time"] = 67890
            registry.write_durable_metadata(session_id, meta)
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(h_lock))

        class _MockUserEnv:
            def DeleteAppContainerProfile(self, name: str) -> int:
                return -2147024894  # 0x80070002: HRESULT_FROM_WIN32(ERROR_FILE_NOT_FOUND)

        from app.skills.sandbox import session_registry
        monkeypatch.setattr(session_registry, "userenv", _MockUserEnv())

        reclaimed = registry.reclaim_orphaned_sessions()
        assert reclaimed == 0
        assert (sessions_dir / f"{session_id}.json").exists()

    def test_profile_deletion_path_not_found_hresult_fails_closed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Unrelated filesystem error 0x80070003 (ERROR_PATH_NOT_FOUND) must NOT be assumed
        to indicate absent profile; it must fail closed and retain metadata.
        """
        sessions_dir = tmp_path / "sessions"
        scratch_base = tmp_path / "scratch"
        registry = SessionRegistry(base_dir=sessions_dir, scratch_base=scratch_base)
        session_id = str(uuid.uuid4())

        trusted_scratch = scratch_base / session_id
        trusted_scratch.mkdir(parents=True, exist_ok=True)

        h_lock = registry.register_session_intent(session_id, scratch_path=trusted_scratch)
        try:
            meta = registry.read_metadata(session_id)
            meta["supervisor_pid"] = 4000000
            meta["supervisor_start_time"] = 12345
            meta["worker_pid"] = 4000004
            meta["worker_start_time"] = 67890
            registry.write_durable_metadata(session_id, meta)
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(h_lock))

        class _MockUserEnv:
            def DeleteAppContainerProfile(self, name: str) -> int:
                return -2147024893  # 0x80070003: HRESULT_FROM_WIN32(ERROR_PATH_NOT_FOUND)

        from app.skills.sandbox import session_registry
        monkeypatch.setattr(session_registry, "userenv", _MockUserEnv())

        reclaimed = registry.reclaim_orphaned_sessions()
        assert reclaimed == 0
        assert (sessions_dir / f"{session_id}.json").exists()

    def test_profile_deletion_exception_fails_closed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When DeleteAppContainerProfile raises an exception, cleanup must fail closed and retain metadata."""
        sessions_dir = tmp_path / "sessions"
        scratch_base = tmp_path / "scratch"
        registry = SessionRegistry(base_dir=sessions_dir, scratch_base=scratch_base)
        session_id = str(uuid.uuid4())

        trusted_scratch = scratch_base / session_id
        trusted_scratch.mkdir(parents=True, exist_ok=True)

        h_lock = registry.register_session_intent(session_id, scratch_path=trusted_scratch)
        try:
            meta = registry.read_metadata(session_id)
            meta["supervisor_pid"] = 4000000
            meta["supervisor_start_time"] = 12345
            meta["worker_pid"] = 4000004
            meta["worker_start_time"] = 67890
            registry.write_durable_metadata(session_id, meta)
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(h_lock))

        class _MockUserEnv:
            def DeleteAppContainerProfile(self, name: str) -> int:
                raise OSError("RPC server unavailable")

        from app.skills.sandbox import session_registry
        monkeypatch.setattr(session_registry, "userenv", _MockUserEnv())

        reclaimed = registry.reclaim_orphaned_sessions()
        assert reclaimed == 0
        assert (sessions_dir / f"{session_id}.json").exists()


# ---------------------------------------------------------------------------
# 7. Scratch Cleanup Verification & Fail-Closed Behavior (Section 2)
# ---------------------------------------------------------------------------

class TestScratchCleanupVerification:
    def test_scratch_cleanup_fails_closed_when_directory_cannot_be_unlinked(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When handle cleanup fails to delete the directory, cleanup fails closed and retains metadata."""
        sessions_dir = tmp_path / "sessions"
        scratch_base = tmp_path / "scratch"
        registry = SessionRegistry(base_dir=sessions_dir, scratch_base=scratch_base)
        session_id = str(uuid.uuid4())

        trusted_scratch = scratch_base / session_id
        trusted_scratch.mkdir(parents=True, exist_ok=True)
        (trusted_scratch / "leftover.bin").write_bytes(b"data")

        h_lock = registry.register_session_intent(session_id, scratch_path=trusted_scratch)
        try:
            meta = registry.read_metadata(session_id)
            meta["supervisor_pid"] = 4000000
            meta["supervisor_start_time"] = 12345
            meta["worker_pid"] = 4000004
            meta["worker_start_time"] = 67890
            registry.write_durable_metadata(session_id, meta)
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(h_lock))

        # Mock delete_file_by_handle to fail
        from app.skills.sandbox import session_registry
        from app.skills.sandbox.win32_acl import FilesystemCleanupError

        def failing_delete(h: Any) -> bool:
            raise FilesystemCleanupError("Mock handle deletion failure")

        monkeypatch.setattr(session_registry, "delete_file_by_handle", failing_delete)

        reclaimed = registry.reclaim_orphaned_sessions()
        assert reclaimed == 0
        assert (sessions_dir / f"{session_id}.json").exists()
        assert trusted_scratch.exists()

    def test_scratch_cleanup_already_absent_succeeds(
        self,
        tmp_path: Path,
    ) -> None:
        """When scratch directory is already absent, scratch is verified absent,
        so cleanup succeeds and metadata is removed.
        """
        sessions_dir = tmp_path / "sessions"
        scratch_base = tmp_path / "scratch"
        registry = SessionRegistry(base_dir=sessions_dir, scratch_base=scratch_base)
        session_id = str(uuid.uuid4())

        # Ensure directory is absent
        trusted_scratch = scratch_base / session_id
        assert not trusted_scratch.exists()

        h_lock = registry.register_session_intent(session_id, scratch_path=trusted_scratch)
        try:
            meta = registry.read_metadata(session_id)
            meta["supervisor_pid"] = 4000000
            meta["supervisor_start_time"] = 12345
            meta["worker_pid"] = 4000004
            meta["worker_start_time"] = 67890
            registry.write_durable_metadata(session_id, meta)
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(h_lock))

        reclaimed = registry.reclaim_orphaned_sessions()
        assert reclaimed == 1
        assert not (sessions_dir / f"{session_id}.json").exists()
        assert (sessions_dir / f"{session_id}.lock").exists()

    def test_scratch_cleanup_successful(
        self,
        tmp_path: Path,
    ) -> None:
        """When scratch directory exists, handle cleanup deletes it directly,
        and session is reclaimed without invoking any path-based cleanup.
        """
        sessions_dir = tmp_path / "sessions"
        scratch_base = tmp_path / "scratch"
        registry = SessionRegistry(base_dir=sessions_dir, scratch_base=scratch_base)
        session_id = str(uuid.uuid4())

        trusted_scratch = scratch_base / session_id
        trusted_scratch.mkdir(parents=True, exist_ok=True)
        (trusted_scratch / "file.txt").write_text("hello", encoding="utf-8")

        h_lock = registry.register_session_intent(session_id, scratch_path=trusted_scratch)
        try:
            meta = registry.read_metadata(session_id)
            meta["supervisor_pid"] = 4000000
            meta["supervisor_start_time"] = 12345
            meta["worker_pid"] = 4000004
            meta["worker_start_time"] = 67890
            registry.write_durable_metadata(session_id, meta)
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(h_lock))

        reclaimed = registry.reclaim_orphaned_sessions()
        assert reclaimed == 1
        assert not trusted_scratch.exists()
        assert not (sessions_dir / f"{session_id}.json").exists()

    def test_scratch_notification_callback_receives_only_session_id(
        self,
        tmp_path: Path,
    ) -> None:
        """on_session_reclaimed receives strictly the session ID string (no filesystem path)."""
        sessions_dir = tmp_path / "sessions"
        scratch_base = tmp_path / "scratch"
        registry = SessionRegistry(base_dir=sessions_dir, scratch_base=scratch_base)
        session_id = str(uuid.uuid4())

        trusted_scratch = scratch_base / session_id
        trusted_scratch.mkdir(parents=True, exist_ok=True)

        h_lock = registry.register_session_intent(session_id, scratch_path=trusted_scratch)
        try:
            meta = registry.read_metadata(session_id)
            meta["supervisor_pid"] = 4000000
            meta["supervisor_start_time"] = 12345
            meta["worker_pid"] = 4000004
            meta["worker_start_time"] = 67890
            registry.write_durable_metadata(session_id, meta)
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(h_lock))

        notified_args: list[Any] = []
        reclaimed = registry.reclaim_orphaned_sessions(
            on_session_reclaimed=lambda sid: notified_args.append(sid)
        )
        assert reclaimed == 1
        assert len(notified_args) == 1
        assert notified_args[0] == session_id
        # Confirm arg is string and not Path
        assert isinstance(notified_args[0], str)
        assert not isinstance(notified_args[0], Path)

    def test_scratch_cleanup_retry_after_failure(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When handle cleanup fails on pass 1, metadata is retained.
        When handle cleanup succeeds on pass 2, session is reclaimed.
        """
        sessions_dir = tmp_path / "sessions"
        scratch_base = tmp_path / "scratch"
        registry = SessionRegistry(base_dir=sessions_dir, scratch_base=scratch_base)
        session_id = str(uuid.uuid4())

        trusted_scratch = scratch_base / session_id
        trusted_scratch.mkdir(parents=True, exist_ok=True)
        (trusted_scratch / "file.txt").write_text("hello", encoding="utf-8")

        h_lock = registry.register_session_intent(session_id, scratch_path=trusted_scratch)
        try:
            meta = registry.read_metadata(session_id)
            meta["supervisor_pid"] = 4000000
            meta["supervisor_start_time"] = 12345
            meta["worker_pid"] = 4000004
            meta["worker_start_time"] = 67890
            registry.write_durable_metadata(session_id, meta)
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(h_lock))

        from app.skills.sandbox import session_registry
        from app.skills.sandbox.win32_acl import FilesystemCleanupError, delete_file_by_handle
        orig_delete = delete_file_by_handle
        should_fail = True

        def flakey_delete(h: Any) -> bool:
            if should_fail:
                kernel32.CloseHandle(wintypes.HANDLE(h.value if isinstance(h, wintypes.HANDLE) else h))
                raise FilesystemCleanupError("temporary failure")
            return orig_delete(h)

        monkeypatch.setattr(session_registry, "delete_file_by_handle", flakey_delete)

        # Pass 1: fails
        r1 = registry.reclaim_orphaned_sessions()
        assert r1 == 0
        assert (sessions_dir / f"{session_id}.json").exists()

        # Pass 2: succeeds
        should_fail = False
        r2 = registry.reclaim_orphaned_sessions()
        assert r2 == 1
        assert not (sessions_dir / f"{session_id}.json").exists()


# ---------------------------------------------------------------------------
# 7. Metadata Native Write Audit & Schema Ranges (Section 3)
# ---------------------------------------------------------------------------

class TestMetadataNativeWriteAndValidation:
    def test_metadata_write_short_write_fails_and_preserves_previous(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When WriteFile reports fewer bytes written than requested, operation fails and preserves old metadata."""
        registry = SessionRegistry(tmp_path)
        session_id = str(uuid.uuid4())

        h_lock = registry.register_session_intent(session_id)
        try:
            original_meta = registry.read_metadata(session_id)

            # Mock WriteFile to write short
            from app.skills.sandbox.win32_acl_types import kernel32
            orig_write_file = kernel32.WriteFile

            def _mock_write_file(h: Any, buf: Any, n_bytes: int, p_written: Any, ov: Any) -> bool:
                # Claim only 2 bytes were written
                p_written._obj.value = 2
                return True

            monkeypatch.setattr(kernel32, "WriteFile", _mock_write_file)

            with pytest.raises(SessionMetadataError, match="WriteFile failed for temporary metadata"):
                registry.write_durable_metadata(session_id, original_meta)

            # Unmock WriteFile and verify original metadata is intact
            monkeypatch.undo()
            restored = registry.read_metadata(session_id)
            assert restored == original_meta
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(h_lock))

    def test_metadata_write_failed_flush_fails_and_preserves_previous(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When FlushFileBuffers fails, write_durable_metadata fails and preserves old metadata."""
        registry = SessionRegistry(tmp_path)
        session_id = str(uuid.uuid4())

        h_lock = registry.register_session_intent(session_id)
        try:
            original_meta = registry.read_metadata(session_id)

            from app.skills.sandbox.win32_acl_types import kernel32

            def _mock_flush(h: Any) -> bool:
                kernel32.SetLastError(5)  # ERROR_ACCESS_DENIED
                return False

            monkeypatch.setattr(kernel32, "FlushFileBuffers", _mock_flush)

            with pytest.raises(SessionMetadataError, match="FlushFileBuffers failed for temporary metadata"):
                registry.write_durable_metadata(session_id, original_meta)

            monkeypatch.undo()
            restored = registry.read_metadata(session_id)
            assert restored == original_meta
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(h_lock))

    def test_metadata_write_failed_move_preserves_previous(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When MoveFileExW fails, write_durable_metadata fails and preserves old metadata."""
        registry = SessionRegistry(tmp_path)
        session_id = str(uuid.uuid4())

        h_lock = registry.register_session_intent(session_id)
        try:
            original_meta = registry.read_metadata(session_id)

            from app.skills.sandbox.win32_acl_types import kernel32

            def _mock_move(src: Any, dst: Any, flags: int) -> bool:
                kernel32.SetLastError(32)  # ERROR_SHARING_VIOLATION
                return False

            monkeypatch.setattr(kernel32, "MoveFileExW", _mock_move)

            with pytest.raises(SessionMetadataError, match="MoveFileExW failed to commit metadata replacement"):
                registry.write_durable_metadata(session_id, original_meta)

            monkeypatch.undo()
            restored = registry.read_metadata(session_id)
            assert restored == original_meta
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(h_lock))

    def test_reject_metadata_booleans_for_pids_and_filetimes(self) -> None:
        session_id = str(uuid.uuid4())
        base = {
            "session_id": session_id,
            "profile_name": f"Anaya.Worker.{session_id}",
            "status": STATUS_PROFILE_PENDING,
        }

        # Boolean in supervisor_pid
        m1 = dict(base, supervisor_pid=True)
        with pytest.raises(SessionMetadataError, match="Invalid supervisor_pid"):
            validate_metadata_schema(m1, expected_session_id=session_id)

        # Boolean in worker_pid
        m2 = dict(base, worker_pid=False)
        with pytest.raises(SessionMetadataError, match="Invalid worker_pid"):
            validate_metadata_schema(m2, expected_session_id=session_id)

        # Boolean in supervisor_start_time
        m3 = dict(base, supervisor_start_time=True)
        with pytest.raises(SessionMetadataError, match="Invalid supervisor_start_time"):
            validate_metadata_schema(m3, expected_session_id=session_id)

        # Boolean in worker_start_time
        m4 = dict(base, worker_start_time=True)
        with pytest.raises(SessionMetadataError, match="Invalid worker_start_time"):
            validate_metadata_schema(m4, expected_session_id=session_id)

    def test_reject_out_of_range_filetime(self) -> None:
        session_id = str(uuid.uuid4())
        base = {
            "session_id": session_id,
            "profile_name": f"Anaya.Worker.{session_id}",
            "status": STATUS_PROFILE_PENDING,
        }

        # Zero FILETIME
        with pytest.raises(SessionMetadataError, match="Invalid supervisor_start_time"):
            validate_metadata_schema(dict(base, supervisor_start_time=0), expected_session_id=session_id)

        # Negative FILETIME
        with pytest.raises(SessionMetadataError, match="Invalid supervisor_start_time"):
            validate_metadata_schema(dict(base, supervisor_start_time=-100), expected_session_id=session_id)

        # Exceeds unsigned 64-bit int
        with pytest.raises(SessionMetadataError, match="Invalid supervisor_start_time"):
            validate_metadata_schema(dict(base, supervisor_start_time=0xFFFFFFFFFFFFFFFF + 1), expected_session_id=session_id)
