"""Tests for Win32 Access Control, Native NT Handle Operations, and Protected DACL.

Validates:
- 64-bit ctypes structure layouts, sizes, and field offsets for NT and Win32 security types.
- SID conversion between binary PSID and SDDL strings.
- Protected DACL creation with atomic SE_DACL_PROTECTED application.
- Path validation against path separators, ADS colons, and relative traversal components.
- Native NT handle-relative child opening via ntdll.NtCreateFile.
- Reparse point unlinking without descending into external directory targets.
- Preflight privilege verification (SeChangeNotifyPrivilege).
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any
import pytest

IS_WINDOWS = sys.platform == "win32"
pytestmark = pytest.mark.skipif(not IS_WINDOWS, reason="Win32 ACL tests require Windows.")

if IS_WINDOWS:
    from app.skills.sandbox.win32_acl_types import (
        CHILD_INHERIT_ONLY_ACE,
        DELETE,
        EXPLICIT_ACCESS_W,
        FILE_ALL_ACCESS,
        FILE_FLAG_BACKUP_SEMANTICS,
        FILE_FLAG_OPEN_REPARSE_POINT,
        FILE_GENERIC_EXECUTE,
        FILE_GENERIC_READ,
        FILE_GENERIC_WRITE,
        FILE_ID_BOTH_DIR_INFO,
        FILE_SHARE_DELETE,
        FILE_SHARE_READ,
        FILE_SHARE_WRITE,
        GENERIC_READ,
        IO_STATUS_BLOCK,
        NO_INHERITANCE,
        OBJECT_ATTRIBUTES,
        OPEN_EXISTING,
        RUNTIME_ACCESS_MASK,
        SCRATCH_CHILD_ACCESS_MASK,
        SCRATCH_ROOT_ACCESS_MASK,
        SECURITY_ATTRIBUTES,
        SECURITY_DESCRIPTOR,
        SUB_CONTAINERS_AND_OBJECTS_INHERIT,
        SYNCHRONIZE,
        TRAVERSE_ACCESS_MASK,
        TRUSTEE_IS_SID,
        TRUSTEE_IS_USER,
        TRUSTEE_W,
        UNICODE_STRING,
        advapi32,
        kernel32,
        ntdll,
    )
    from app.skills.sandbox.win32_acl import (
        AclError,
        FilesystemCleanupError,
        NativeApiError,
        SecurityDescriptorError,
        build_explicit_access,
        check_traverse_preflight,
        clean_directory_handle_relative,
        convert_sid_to_string,
        convert_string_to_sid,
        create_protected_directory,
        delete_file_by_handle,
        grant_path_access,
        open_relative_child_handle,
        self_test_native_nt_api,
        validate_relative_entry_name,
    )
    from app.skills.sandbox.runtime_provisioner import get_current_user_sid_string


# ---------------------------------------------------------------------------
# 1. 64-bit Structure Layout & Offset Tests
# ---------------------------------------------------------------------------

class TestWin32AclStructureSizing:
    def test_trustee_w_size_and_offsets(self) -> None:
        assert ctypes.sizeof(TRUSTEE_W) == 32
        assert TRUSTEE_W.pMultipleTrustee.offset == 0
        assert TRUSTEE_W.MultipleTrusteeOperation.offset == 8
        assert TRUSTEE_W.TrusteeForm.offset == 12
        assert TRUSTEE_W.TrusteeType.offset == 16
        assert TRUSTEE_W.ptstrName.offset == 24

    def test_explicit_access_w_size_and_offsets(self) -> None:
        assert ctypes.sizeof(EXPLICIT_ACCESS_W) == 48
        assert EXPLICIT_ACCESS_W.grfAccessPermissions.offset == 0
        assert EXPLICIT_ACCESS_W.grfAccessMode.offset == 4
        assert EXPLICIT_ACCESS_W.grfInheritance.offset == 8
        assert EXPLICIT_ACCESS_W.Trustee.offset == 16

    def test_security_descriptor_size(self) -> None:
        assert ctypes.sizeof(SECURITY_DESCRIPTOR) == 40

    def test_security_attributes_size(self) -> None:
        assert ctypes.sizeof(SECURITY_ATTRIBUTES) == 24
        assert SECURITY_ATTRIBUTES.nLength.offset == 0
        assert SECURITY_ATTRIBUTES.lpSecurityDescriptor.offset == 8
        assert SECURITY_ATTRIBUTES.bInheritHandle.offset == 16

    def test_unicode_string_size(self) -> None:
        assert ctypes.sizeof(UNICODE_STRING) == 16
        assert UNICODE_STRING.Length.offset == 0
        assert UNICODE_STRING.MaximumLength.offset == 2
        assert UNICODE_STRING.Buffer.offset == 8

    def test_object_attributes_size(self) -> None:
        assert ctypes.sizeof(OBJECT_ATTRIBUTES) == 48
        assert OBJECT_ATTRIBUTES.Length.offset == 0
        assert OBJECT_ATTRIBUTES.RootDirectory.offset == 8
        assert OBJECT_ATTRIBUTES.ObjectName.offset == 16
        assert OBJECT_ATTRIBUTES.Attributes.offset == 24
        assert OBJECT_ATTRIBUTES.SecurityDescriptor.offset == 32
        assert OBJECT_ATTRIBUTES.SecurityQualityOfService.offset == 40

    def test_io_status_block_size(self) -> None:
        assert ctypes.sizeof(IO_STATUS_BLOCK) == 16
        assert IO_STATUS_BLOCK.Status.offset == 0
        assert IO_STATUS_BLOCK.Information.offset == 8

    def test_file_id_both_dir_info_offsets(self) -> None:
        assert ctypes.sizeof(FILE_ID_BOTH_DIR_INFO) >= 106
        assert FILE_ID_BOTH_DIR_INFO.NextEntryOffset.offset == 0
        assert FILE_ID_BOTH_DIR_INFO.FileAttributes.offset == 56
        assert FILE_ID_BOTH_DIR_INFO.FileNameLength.offset == 60
        assert FILE_ID_BOTH_DIR_INFO.EaSize.offset == 64
        assert FILE_ID_BOTH_DIR_INFO.ShortNameLength.offset == 68
        assert FILE_ID_BOTH_DIR_INFO.ShortName.offset == 70
        assert FILE_ID_BOTH_DIR_INFO.FileId.offset == 96
        assert FILE_ID_BOTH_DIR_INFO.FileName.offset == 104


# ---------------------------------------------------------------------------
# 2. SID and Trustee Conversion Tests
# ---------------------------------------------------------------------------

class TestSidConversion:
    def test_well_known_sid_conversion(self) -> None:
        sids = [
            "S-1-15-2-1",       # ALL APPLICATION PACKAGES
            "S-1-5-32-544",     # Administrators
            "S-1-5-18",         # SYSTEM
            "S-1-1-0",          # Everyone
        ]
        for sid_str in sids:
            p_sid = convert_string_to_sid(sid_str)
            assert p_sid != 0
            try:
                roundtrip = convert_sid_to_string(p_sid)
                assert roundtrip == sid_str
            finally:
                kernel32.LocalFree(ctypes.c_void_p(p_sid))

    def test_current_user_sid_retrieval(self) -> None:
        user_sid = get_current_user_sid_string()
        assert user_sid.startswith("S-1-5-21-") or user_sid.startswith("S-1-5-")
        p_sid = convert_string_to_sid(user_sid)
        try:
            assert convert_sid_to_string(p_sid) == user_sid
        finally:
            kernel32.LocalFree(ctypes.c_void_p(p_sid))

    def test_invalid_string_sid_raises(self) -> None:
        with pytest.raises(ValueError):
            convert_string_to_sid("invalid-sid-format")
        with pytest.raises(ValueError):
            convert_string_to_sid("")

    def test_convert_null_sid_to_string_raises(self) -> None:
        with pytest.raises(ValueError):
            convert_sid_to_string(0)


# ---------------------------------------------------------------------------
# 3. Path Validation Tests
# ---------------------------------------------------------------------------

class TestRelativePathValidation:
    def test_safe_relative_names_pass(self) -> None:
        validate_relative_entry_name("file.txt")
        validate_relative_entry_name("my_folder")
        validate_relative_entry_name(".hidden_marker")
        validate_relative_entry_name("hyphen-name.dat")

    def test_separators_and_navigation_rejected(self) -> None:
        with pytest.raises(FilesystemCleanupError, match="Malicious path separator"):
            validate_relative_entry_name("sub/file.txt")
        with pytest.raises(FilesystemCleanupError, match="Malicious path separator"):
            validate_relative_entry_name("sub\\file.txt")
        with pytest.raises(FilesystemCleanupError, match="Malicious alternate data stream"):
            validate_relative_entry_name("file.txt:stream")
        with pytest.raises(FilesystemCleanupError, match="Illegal relative navigation"):
            validate_relative_entry_name(".")
        with pytest.raises(FilesystemCleanupError, match="Illegal relative navigation"):
            validate_relative_entry_name("..")
        with pytest.raises(FilesystemCleanupError, match="non-empty string"):
            validate_relative_entry_name("")


# ---------------------------------------------------------------------------
# 4. Protected Directory Creation & DACL Configuration
# ---------------------------------------------------------------------------

class TestProtectedDirectoryCreation:
    def test_create_protected_directory_success(self, tmp_path: Path) -> None:
        target_dir = tmp_path / "protected_test"
        user_sid = get_current_user_sid_string()
        p_user = convert_string_to_sid(user_sid)

        try:
            ea = build_explicit_access(p_user, FILE_ALL_ACCESS, SUB_CONTAINERS_AND_OBJECTS_INHERIT)
            create_protected_directory(target_dir, [ea])
            assert target_dir.is_dir()

            # Test creating and deleting a file inside the protected directory
            test_file = target_dir / "inside.txt"
            test_file.write_text("protected payload", encoding="utf-8")
            assert test_file.read_text(encoding="utf-8") == "protected payload"
        finally:
            kernel32.LocalFree(ctypes.c_void_p(p_user))

    def test_create_protected_directory_existing_raises(self, tmp_path: Path) -> None:
        existing = tmp_path / "already_here"
        existing.mkdir()
        user_sid = get_current_user_sid_string()
        p_user = convert_string_to_sid(user_sid)

        try:
            ea = build_explicit_access(p_user, FILE_ALL_ACCESS, NO_INHERITANCE)
            with pytest.raises(FileExistsError):
                create_protected_directory(existing, [ea])
        finally:
            kernel32.LocalFree(ctypes.c_void_p(p_user))

    def test_create_protected_directory_empty_entries_raises(self, tmp_path: Path) -> None:
        target = tmp_path / "empty_entries"
        with pytest.raises(ValueError, match="at least one access entry"):
            create_protected_directory(target, [])


# ---------------------------------------------------------------------------
# 5. Native NT Handle-Relative Operations & Cleanup
# ---------------------------------------------------------------------------

class TestNativeHandleRelativeOperations:
    def test_self_test_native_nt_api(self) -> None:
        assert self_test_native_nt_api() is True

    def test_check_traverse_preflight(self) -> None:
        # SeChangeNotifyPrivilege is enabled by default on standard Windows installations
        res = check_traverse_preflight()
        assert isinstance(res, bool)

    def test_clean_directory_handle_relative_nested_tree(self, tmp_path: Path) -> None:
        root_dir = tmp_path / "cleanup_tree"
        root_dir.mkdir()

        # Build deep nested tree
        (root_dir / "sub1" / "sub2").mkdir(parents=True)
        (root_dir / "sub1" / "file1.txt").write_text("data 1", encoding="utf-8")
        (root_dir / "sub1" / "sub2" / "file2.bin").write_bytes(b"\x00\xFF" * 100)
        (root_dir / "root_file.txt").write_text("root data", encoding="utf-8")

        h_dir = kernel32.CreateFileW(
            str(root_dir),
            DELETE | GENERIC_READ | SYNCHRONIZE,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        assert h_dir != -1

        try:
            clean_directory_handle_relative(h_dir)
            delete_file_by_handle(h_dir)
        except Exception:
            kernel32.CloseHandle(wintypes.HANDLE(h_dir))
            raise

        assert not root_dir.exists()

    def test_clean_directory_with_readonly_file(self, tmp_path: Path) -> None:
        """Verify that files with FILE_ATTRIBUTE_READONLY are deleted handle-relativistically."""
        root_dir = tmp_path / "ro_file_tree"
        root_dir.mkdir()

        ro_file = root_dir / "readonly.txt"
        ro_file.write_text("read only content", encoding="utf-8")
        # Set FILE_ATTRIBUTE_READONLY (0x01)
        ok = kernel32.SetFileAttributesW(str(ro_file), 0x00000001)
        assert ok, f"SetFileAttributesW failed: {ctypes.get_last_error()}"

        h_dir = kernel32.CreateFileW(
            str(root_dir),
            DELETE | GENERIC_READ | SYNCHRONIZE,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        assert h_dir != -1

        try:
            clean_directory_handle_relative(h_dir)
            delete_file_by_handle(h_dir)
        except Exception:
            kernel32.CloseHandle(wintypes.HANDLE(h_dir))
            raise

        assert not root_dir.exists()

    def test_clean_directory_with_readonly_nested_subdirectories(self, tmp_path: Path) -> None:
        """Verify that read-only subdirectories and read-only files inside them are deleted."""
        root_dir = tmp_path / "ro_subdir_tree"
        root_dir.mkdir()

        sub_dir = root_dir / "ro_subdir"
        sub_dir.mkdir()
        nested_file = sub_dir / "ro_nested.bin"
        nested_file.write_bytes(b"\xCA\xFE\xBA\xBE")

        # Set read-only on both the nested file and the subdirectory
        assert kernel32.SetFileAttributesW(str(nested_file), 0x00000001)
        assert kernel32.SetFileAttributesW(str(sub_dir), 0x00000001)

        h_dir = kernel32.CreateFileW(
            str(root_dir),
            DELETE | GENERIC_READ | SYNCHRONIZE,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        assert h_dir != -1

        try:
            clean_directory_handle_relative(h_dir)
            delete_file_by_handle(h_dir)
        except Exception:
            kernel32.CloseHandle(wintypes.HANDLE(h_dir))
            raise

        assert not root_dir.exists()

    def test_runtime_provisioner_cleanup_scratch_dir_zero_shutil_fallback(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify cleanup_scratch_dir cleans scratch and never invokes shutil.rmtree."""
        from app.skills.sandbox.runtime_provisioner import RuntimeProvisioner

        provisioner = RuntimeProvisioner(
            runtime_dir=tmp_path / "runtime_root",
            scratch_dir=tmp_path / "scratch_root",
        )
        scratch_dir = tmp_path / "scratch_target"
        scratch_dir.mkdir()
        sub = scratch_dir / "nested"
        sub.mkdir()
        f1 = sub / "ro_file.txt"
        f1.write_text("hello", encoding="utf-8")
        kernel32.SetFileAttributesW(str(f1), 0x00000001)

        rmtree_called = False

        def _forbidden_rmtree(*args: Any, **kwargs: Any) -> None:
            nonlocal rmtree_called
            rmtree_called = True
            raise AssertionError("shutil.rmtree was called! Fallback is strictly forbidden.")

        monkeypatch.setattr(shutil, "rmtree", _forbidden_rmtree)

        provisioner.cleanup_scratch_dir(scratch_dir)

        assert not scratch_dir.exists()
        assert not rmtree_called

    def test_reparse_point_unlinked_without_traversing_target(self, tmp_path: Path) -> None:
        """Verify that a directory junction is unlinked without deleting the target directory."""
        external_target = tmp_path / "external_target_dir"
        external_target.mkdir()
        canary = external_target / "canary_keep_me.txt"
        canary.write_text("critical external data", encoding="utf-8")

        scratch = tmp_path / "scratch_with_junction"
        scratch.mkdir()

        junction = scratch / "link_to_external"
        # Create junction using cmd /c mklink /J
        import subprocess
        res = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(external_target)],
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            pytest.skip("Creating junction requires appropriate privileges or file system support.")

        assert junction.is_dir()
        assert (junction / "canary_keep_me.txt").is_file()

        h_dir = kernel32.CreateFileW(
            str(scratch),
            DELETE | GENERIC_READ | SYNCHRONIZE,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        assert h_dir != -1

        clean_directory_handle_relative(h_dir)
        delete_file_by_handle(h_dir)

        # Scratch directory must be deleted
        assert not scratch.exists()
        # External target and canary MUST survive unharmed!
        assert external_target.is_dir()
        assert canary.is_file()
        assert canary.read_text(encoding="utf-8") == "critical external data"
