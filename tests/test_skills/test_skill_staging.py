"""Comprehensive test suite for secure external skill staging (Phase 10.4.3).

Covers:
- Safe package staging into session scratch directories
- Handle-relative traversal preserving directory boundaries
- Rejection of symlinks, junctions, and reparse points
- Enforcement of strict resource bounds (file count, file size, total package size, depth)
- Disallowed file types and protected module name shadowing protection
- Detection of in-place file modifications during staging (TOCTOU protection)
- Read-only permission and DACL application to staged code
- Clean reclamation during session teardown
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest.mock as mock
import pytest

from app.skills.staging import (
    MAX_FILENAME_LENGTH,
    MAX_SOURCE_FILE_BYTES,
    MAX_STAGED_FILES,
    MAX_STAGING_DEPTH,
    MAX_TOTAL_PACKAGE_BYTES,
    PROTECTED_MODULE_NAMES,
    StagedPackage,
    StagingError,
    stage_external_package,
)

IS_WINDOWS = sys.platform == "win32"


def _create_minimal_manifest(pkg_name: str, entrypoint: str = "main:MySkill") -> str:
    """Helper to generate a valid minimal skill.json."""
    return json.dumps({
        "schema_version": 1,
        "id": f"user.{pkg_name}",
        "name": f"Test {pkg_name}",
        "description": "A test external skill package.",
        "version": "1.0.0",
        "entrypoint": entrypoint,
    })


class TestStagingValidation:
    """Tests for package validation, manifest verification, and valid staging."""

    def test_stage_valid_single_module_package(self, tmp_path: Path):
        """Single module valid package is successfully staged into session scratch / package."""
        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        pkg_dir = skills_root / "hello_tool"
        pkg_dir.mkdir()

        manifest_content = _create_minimal_manifest("hello_tool", "main:HelloSkill")
        (pkg_dir / "skill.json").write_text(manifest_content, encoding="utf-8")
        (pkg_dir / "main.py").write_text("class HelloSkill:\n    pass\n", encoding="utf-8")

        scratch_dir = tmp_path / "scratch"
        scratch_dir.mkdir()

        staged = stage_external_package(pkg_dir, scratch_dir, skills_root=skills_root)

        assert isinstance(staged, StagedPackage)
        assert staged.skill_id == "user.hello_tool"
        assert staged.entrypoint_module == "main"
        assert staged.entrypoint_class == "HelloSkill"
        assert staged.file_count == 2
        assert staged.staged_dir == scratch_dir / "package"
        assert (staged.staged_dir / "skill.json").is_file()
        assert (staged.staged_dir / "main.py").is_file()
        assert (staged.staged_dir / "main.py").read_text(encoding="utf-8") == "class HelloSkill:\n    pass\n"

    def test_stage_valid_multi_module_package(self, tmp_path: Path):
        """Package with multiple nested Python modules within depth limit is staged correctly."""
        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        pkg_dir = skills_root / "complex_tool"
        pkg_dir.mkdir()

        manifest_content = _create_minimal_manifest("complex_tool", "main:ComplexSkill")
        (pkg_dir / "skill.json").write_text(manifest_content, encoding="utf-8")
        (pkg_dir / "main.py").write_text("import helper\nclass ComplexSkill:\n    pass\n", encoding="utf-8")
        (pkg_dir / "helper.py").write_text("VALUE = 42\n", encoding="utf-8")

        sub_dir = pkg_dir / "sub"
        sub_dir.mkdir()
        (sub_dir / "__init__.py").write_text("", encoding="utf-8")
        (sub_dir / "calc.py").write_text("def add(a, b): return a + b\n", encoding="utf-8")

        scratch_dir = tmp_path / "scratch"
        scratch_dir.mkdir()

        staged = stage_external_package(pkg_dir, scratch_dir, skills_root=skills_root)

        assert staged.file_count == 5
        assert (staged.staged_dir / "main.py").is_file()
        assert (staged.staged_dir / "helper.py").is_file()
        assert (staged.staged_dir / "sub" / "__init__.py").is_file()
        assert (staged.staged_dir / "sub" / "calc.py").is_file()

    def test_missing_manifest_fails_closed(self, tmp_path: Path):
        """Package lacking skill.json is rejected with missing_manifest."""
        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        pkg_dir = skills_root / "no_manifest"
        pkg_dir.mkdir()
        (pkg_dir / "main.py").write_text("pass", encoding="utf-8")

        scratch_dir = tmp_path / "scratch"
        scratch_dir.mkdir()

        with pytest.raises(StagingError) as exc_info:
            stage_external_package(pkg_dir, scratch_dir, skills_root=skills_root)
        assert exc_info.value.error_code == "missing_manifest"
        assert not (scratch_dir / "package").exists()

    def test_missing_entrypoint_module_fails_closed(self, tmp_path: Path):
        """Package declaring an entrypoint module that is absent fails closed with entrypoint_not_found."""
        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        pkg_dir = skills_root / "bad_entry"
        pkg_dir.mkdir()

        manifest_content = _create_minimal_manifest("bad_entry", "runner:RunSkill")
        (pkg_dir / "skill.json").write_text(manifest_content, encoding="utf-8")
        (pkg_dir / "other.py").write_text("pass", encoding="utf-8")

        scratch_dir = tmp_path / "scratch"
        scratch_dir.mkdir()

        with pytest.raises(StagingError) as exc_info:
            stage_external_package(pkg_dir, scratch_dir, skills_root=skills_root)
        assert exc_info.value.error_code == "entrypoint_not_found"
        assert not (scratch_dir / "package").exists()

    def test_identity_mismatch_fails_closed(self, tmp_path: Path):
        """Package directory name not matching manifest ID fails closed."""
        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        pkg_dir = skills_root / "actual_name"
        pkg_dir.mkdir()

        manifest_content = json.dumps({
            "schema_version": 1,
            "id": "user.different_name",
            "name": "Mismatched",
            "description": "Mismatch test",
            "version": "1.0.0",
            "entrypoint": "main:Skill",
        })
        (pkg_dir / "skill.json").write_text(manifest_content, encoding="utf-8")
        (pkg_dir / "main.py").write_text("pass", encoding="utf-8")

        scratch_dir = tmp_path / "scratch"
        scratch_dir.mkdir()

        with pytest.raises(StagingError) as exc_info:
            stage_external_package(pkg_dir, scratch_dir, skills_root=skills_root)
        assert exc_info.value.error_code == "identity_mismatch"
        assert not (scratch_dir / "package").exists()

    def test_malformed_manifest_json_fails_closed(self, tmp_path: Path):
        """Package with invalid JSON in skill.json fails closed with manifest_validation_error."""
        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        pkg_dir = skills_root / "bad_json"
        pkg_dir.mkdir()

        (pkg_dir / "skill.json").write_text("{not valid json", encoding="utf-8")
        (pkg_dir / "main.py").write_text("pass", encoding="utf-8")

        scratch_dir = tmp_path / "scratch"
        scratch_dir.mkdir()

        with pytest.raises(StagingError) as exc_info:
            stage_external_package(pkg_dir, scratch_dir, skills_root=skills_root)
        assert exc_info.value.error_code == "manifest_validation_error"

    def test_nonexistent_scratch_dir_fails_closed(self, tmp_path: Path):
        """Non-existent scratch directory fails closed immediately."""
        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        pkg_dir = skills_root / "test_pkg"
        pkg_dir.mkdir()
        (pkg_dir / "skill.json").write_text(_create_minimal_manifest("test_pkg"), encoding="utf-8")
        (pkg_dir / "main.py").write_text("pass", encoding="utf-8")

        scratch_dir = tmp_path / "does_not_exist"

        with pytest.raises(StagingError) as exc_info:
            stage_external_package(pkg_dir, scratch_dir, skills_root=skills_root)
        assert exc_info.value.error_code == "scratch_dir_not_found"

    def test_target_package_already_exists_fails_closed(self, tmp_path: Path):
        """Pre-existing scratch/package directory fails closed with staging_conflict."""
        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        pkg_dir = skills_root / "conflict_pkg"
        pkg_dir.mkdir()
        (pkg_dir / "skill.json").write_text(_create_minimal_manifest("conflict_pkg"), encoding="utf-8")
        (pkg_dir / "main.py").write_text("pass", encoding="utf-8")

        scratch_dir = tmp_path / "scratch"
        scratch_dir.mkdir()
        (scratch_dir / "package").mkdir()

        with pytest.raises(StagingError) as exc_info:
            stage_external_package(pkg_dir, scratch_dir, skills_root=skills_root)
        assert exc_info.value.error_code == "staging_conflict"


class TestStagingResourceLimits:
    """Tests strictly verifying resource bounds (file size, total size, file count, depth, filename)."""

    def test_individual_file_exceeds_max_source_file_bytes(self, tmp_path: Path):
        """Individual source file exceeding 256 KiB is rejected with file_too_large."""
        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        pkg_dir = skills_root / "large_file_pkg"
        pkg_dir.mkdir()

        (pkg_dir / "skill.json").write_text(_create_minimal_manifest("large_file_pkg"), encoding="utf-8")
        # 256 KiB + 1 byte
        (pkg_dir / "main.py").write_bytes(b"#" * (MAX_SOURCE_FILE_BYTES + 1))

        scratch_dir = tmp_path / "scratch"
        scratch_dir.mkdir()

        with pytest.raises(StagingError) as exc_info:
            stage_external_package(pkg_dir, scratch_dir, skills_root=skills_root)
        assert exc_info.value.error_code == "file_too_large"
        assert not (scratch_dir / "package").exists()

    def test_package_exceeds_max_total_bytes(self, tmp_path: Path):
        """Package with total size exceeding 2 MiB is rejected with package_too_large."""
        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        pkg_dir = skills_root / "large_pkg"
        pkg_dir.mkdir()

        (pkg_dir / "skill.json").write_text(_create_minimal_manifest("large_pkg"), encoding="utf-8")
        # 9 files of 240 KiB each = 2160 KiB > 2048 KiB (2 MiB)
        file_payload = b"#" * (240 * 1024)
        for i in range(9):
            (pkg_dir / f"mod_{i}.py").write_bytes(file_payload)
        (pkg_dir / "main.py").write_text("pass", encoding="utf-8")

        scratch_dir = tmp_path / "scratch"
        scratch_dir.mkdir()

        with pytest.raises(StagingError) as exc_info:
            stage_external_package(pkg_dir, scratch_dir, skills_root=skills_root)
        assert exc_info.value.error_code == "package_too_large"
        assert not (scratch_dir / "package").exists()

    def test_file_count_exceeds_limit(self, tmp_path: Path):
        """Package with more than 50 files is rejected with file_count_exceeded."""
        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        pkg_dir = skills_root / "many_files"
        pkg_dir.mkdir()

        (pkg_dir / "skill.json").write_text(_create_minimal_manifest("many_files"), encoding="utf-8")
        (pkg_dir / "main.py").write_text("pass", encoding="utf-8")
        # Add 50 more files -> total files = 52 > 50
        for i in range(50):
            (pkg_dir / f"extra_{i}.py").write_text("pass", encoding="utf-8")

        scratch_dir = tmp_path / "scratch"
        scratch_dir.mkdir()

        with pytest.raises(StagingError) as exc_info:
            stage_external_package(pkg_dir, scratch_dir, skills_root=skills_root)
        assert exc_info.value.error_code == "file_count_exceeded"
        assert not (scratch_dir / "package").exists()

    def test_directory_depth_exceeds_limit(self, tmp_path: Path):
        """Subdirectories deeper than MAX_STAGING_DEPTH (3) are rejected with directory_depth_exceeded."""
        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        pkg_dir = skills_root / "deep_pkg"
        pkg_dir.mkdir()

        (pkg_dir / "skill.json").write_text(_create_minimal_manifest("deep_pkg"), encoding="utf-8")
        (pkg_dir / "main.py").write_text("pass", encoding="utf-8")

        # Depth 1: d1, Depth 2: d2, Depth 3: d3, Depth 4: d4 -> exceeds 3
        deep_dir = pkg_dir / "d1" / "d2" / "d3" / "d4"
        deep_dir.mkdir(parents=True)
        (deep_dir / "sub.py").write_text("pass", encoding="utf-8")

        scratch_dir = tmp_path / "scratch"
        scratch_dir.mkdir()

        with pytest.raises(StagingError) as exc_info:
            stage_external_package(pkg_dir, scratch_dir, skills_root=skills_root)
        assert exc_info.value.error_code == "directory_depth_exceeded"
        assert not (scratch_dir / "package").exists()

    def test_filename_length_exceeds_limit(self, tmp_path: Path):
        """Filename longer than 64 characters is rejected with invalid_filename."""
        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        pkg_dir = skills_root / "long_name_pkg"
        pkg_dir.mkdir()

        (pkg_dir / "skill.json").write_text(_create_minimal_manifest("long_name_pkg"), encoding="utf-8")
        (pkg_dir / "main.py").write_text("pass", encoding="utf-8")
        long_filename = "a" * 65 + ".py"
        (pkg_dir / long_filename).write_text("pass", encoding="utf-8")

        scratch_dir = tmp_path / "scratch"
        scratch_dir.mkdir()

        with pytest.raises(StagingError) as exc_info:
            stage_external_package(pkg_dir, scratch_dir, skills_root=skills_root)
        assert exc_info.value.error_code == "invalid_filename"
        assert not (scratch_dir / "package").exists()

    def test_disallowed_file_extension(self, tmp_path: Path):
        """Files with extensions other than .py or skill.json are rejected."""
        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        pkg_dir = skills_root / "binary_pkg"
        pkg_dir.mkdir()

        (pkg_dir / "skill.json").write_text(_create_minimal_manifest("binary_pkg"), encoding="utf-8")
        (pkg_dir / "main.py").write_text("pass", encoding="utf-8")
        (pkg_dir / "malicious.exe").write_bytes(b"MZ\x00\x00")

        scratch_dir = tmp_path / "scratch"
        scratch_dir.mkdir()

        with pytest.raises(StagingError) as exc_info:
            stage_external_package(pkg_dir, scratch_dir, skills_root=skills_root)
        assert exc_info.value.error_code == "disallowed_file_extension"
        assert not (scratch_dir / "package").exists()


class TestStagingSecurityGuarantees:
    """Security tests verifying symlink/junction defense, shadowing prevention, and mutation detection."""

    @pytest.mark.parametrize("mod_name", ["sys", "os", "app", "subprocess", "socket", "ctypes"])
    def test_shadowing_protected_module_fails_closed(self, tmp_path: Path, mod_name: str):
        """Packages attempting to include files named after protected modules are rejected."""
        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        pkg_dir = skills_root / f"shadow_{mod_name}"
        pkg_dir.mkdir()

        (pkg_dir / "skill.json").write_text(_create_minimal_manifest(f"shadow_{mod_name}"), encoding="utf-8")
        (pkg_dir / "main.py").write_text("pass", encoding="utf-8")
        (pkg_dir / f"{mod_name}.py").write_text("# shadow payload", encoding="utf-8")

        scratch_dir = tmp_path / "scratch"
        scratch_dir.mkdir()

        with pytest.raises(StagingError) as exc_info:
            stage_external_package(pkg_dir, scratch_dir, skills_root=skills_root)
        assert exc_info.value.error_code == "shadowing_disallowed"
        assert not (scratch_dir / "package").exists()

    @pytest.mark.skipif(not hasattr(os, "symlink"), reason="Symlinks not supported on platform")
    def test_symlink_source_file_rejected(self, tmp_path: Path):
        """A package containing a symlinked source file is rejected with reparse_point_detected."""
        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        pkg_dir = skills_root / "symlink_file_pkg"
        pkg_dir.mkdir()

        (pkg_dir / "skill.json").write_text(_create_minimal_manifest("symlink_file_pkg"), encoding="utf-8")
        (pkg_dir / "main.py").write_text("pass", encoding="utf-8")

        target = tmp_path / "external_target.py"
        target.write_text("secret = 'host_data'", encoding="utf-8")

        link = pkg_dir / "helper.py"
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            pytest.skip("Symlink creation not permitted in this environment")

        scratch_dir = tmp_path / "scratch"
        scratch_dir.mkdir()

        with pytest.raises(StagingError) as exc_info:
            stage_external_package(pkg_dir, scratch_dir, skills_root=skills_root)
        assert exc_info.value.error_code == "reparse_point_detected"
        assert not (scratch_dir / "package").exists()

    @pytest.mark.skipif(not hasattr(os, "symlink"), reason="Symlinks not supported on platform")
    def test_symlink_package_directory_rejected(self, tmp_path: Path):
        """A package directory that is itself a symlink is rejected."""
        skills_root = tmp_path / "skills"
        skills_root.mkdir()

        real_pkg = tmp_path / "real_pkg"
        real_pkg.mkdir()
        (real_pkg / "skill.json").write_text(_create_minimal_manifest("sym_pkg"), encoding="utf-8")
        (real_pkg / "main.py").write_text("pass", encoding="utf-8")

        sym_pkg = skills_root / "sym_pkg"
        try:
            os.symlink(real_pkg, sym_pkg, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("Symlink directory creation not permitted")

        scratch_dir = tmp_path / "scratch"
        scratch_dir.mkdir()

        with pytest.raises(StagingError) as exc_info:
            stage_external_package(sym_pkg, scratch_dir, skills_root=skills_root)
        assert exc_info.value.error_code == "reparse_point_detected"
        assert not (scratch_dir / "package").exists()

    def test_mutation_detected_during_read_posix(self, tmp_path: Path):
        """Simulated file mutation on POSIX during staging triggers package_modified_during_staging."""
        if IS_WINDOWS:
            pytest.skip("POSIX-specific test")

        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        pkg_dir = skills_root / "mutate_pkg"
        pkg_dir.mkdir()

        (pkg_dir / "skill.json").write_text(_create_minimal_manifest("mutate_pkg"), encoding="utf-8")
        (pkg_dir / "main.py").write_text("initial content", encoding="utf-8")

        scratch_dir = tmp_path / "scratch"
        scratch_dir.mkdir()

        orig_fstat = os.fstat
        call_count = 0

        def mutating_fstat(fd):
            nonlocal call_count
            res = orig_fstat(fd)
            call_count += 1
            if call_count >= 2:
                # Return modified mtime for post-read check
                fake_stat = os.stat_result((
                    res.st_mode,
                    res.st_ino,
                    res.st_dev,
                    res.st_nlink,
                    res.st_uid,
                    res.st_gid,
                    res.st_size,
                    res.st_atime,
                    res.st_mtime + 100,  # Modified mtime!
                    res.st_ctime,
                ))
                return fake_stat
            return res

        with mock.patch("os.fstat", side_effect=mutating_fstat):
            with pytest.raises(StagingError) as exc_info:
                stage_external_package(pkg_dir, scratch_dir, skills_root=skills_root)
            assert exc_info.value.error_code == "package_modified_during_staging"

    @pytest.mark.skipif(not IS_WINDOWS, reason="Windows-specific test")
    def test_mutation_detected_during_read_windows(self, tmp_path: Path):
        """Simulated file mutation on Windows during staging triggers package_modified_during_staging."""
        from app.skills.sandbox.win32_acl_types import FILE_BASIC_INFO, FileBasicInfo
        import ctypes

        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        pkg_dir = skills_root / "win_mutate_pkg"
        pkg_dir.mkdir()

        (pkg_dir / "skill.json").write_text(_create_minimal_manifest("win_mutate_pkg"), encoding="utf-8")
        (pkg_dir / "main.py").write_text("initial content", encoding="utf-8")

        scratch_dir = tmp_path / "scratch"
        scratch_dir.mkdir()

        import app.skills.staging as staging_mod
        orig_get_info = staging_mod.kernel32.GetFileInformationByHandleEx
        call_count = 0

        def mutating_get_info(h_file, info_class, lp_buf, buf_size):
            res = orig_get_info(h_file, info_class, lp_buf, buf_size)
            nonlocal call_count
            if res and info_class == FileBasicInfo:
                call_count += 1
                if call_count >= 2:
                    # Modify LastWriteTime in post-read check
                    basic = ctypes.cast(lp_buf, ctypes.POINTER(FILE_BASIC_INFO)).contents
                    basic.LastWriteTime = basic.LastWriteTime + 10000000
            return res

        with mock.patch.object(staging_mod.kernel32, "GetFileInformationByHandleEx", side_effect=mutating_get_info):
            with pytest.raises(StagingError) as exc_info:
                stage_external_package(pkg_dir, scratch_dir, skills_root=skills_root)
            assert exc_info.value.error_code == "package_modified_during_staging"

    @pytest.mark.skipif(not IS_WINDOWS, reason="Windows-specific AppContainer SID test")
    def test_staging_with_appcontainer_sid_windows(self, tmp_path: Path):
        """Passing worker_sid on Windows invokes DACL application granting worker Read & Execute."""
        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        pkg_dir = skills_root / "sid_pkg"
        pkg_dir.mkdir()

        (pkg_dir / "skill.json").write_text(_create_minimal_manifest("sid_pkg"), encoding="utf-8")
        (pkg_dir / "main.py").write_text("pass", encoding="utf-8")

        scratch_dir = tmp_path / "scratch"
        scratch_dir.mkdir()

        # Well-known AppContainer SID format
        test_sid = "S-1-15-2-1-2-3-4-5-6-7"
        with mock.patch("app.skills.staging._apply_read_only_acl_windows") as mock_apply:
            staged = stage_external_package(pkg_dir, scratch_dir, worker_sid=test_sid, skills_root=skills_root)
            assert mock_apply.called
            assert mock_apply.call_args[0][0] == staged.staged_dir
            assert mock_apply.call_args[0][1] == test_sid

    def test_staged_files_read_only_mode(self, tmp_path: Path):
        """Staged files and directories are made read-only."""
        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        pkg_dir = skills_root / "ro_pkg"
        pkg_dir.mkdir()

        (pkg_dir / "skill.json").write_text(_create_minimal_manifest("ro_pkg"), encoding="utf-8")
        (pkg_dir / "main.py").write_text("pass", encoding="utf-8")

        scratch_dir = tmp_path / "scratch"
        scratch_dir.mkdir()

        staged = stage_external_package(pkg_dir, scratch_dir, skills_root=skills_root)

        # On POSIX, file permissions should have no write bits
        if not IS_WINDOWS:
            st = os.stat(staged.staged_dir / "main.py")
            assert (st.st_mode & stat.S_IWUSR) == 0

    def test_session_cleanup_removes_staged_content(self, tmp_path: Path):
        """Session scratch directory containing staged files can be cleanly deleted."""
        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        pkg_dir = skills_root / "cleanup_pkg"
        pkg_dir.mkdir()

        (pkg_dir / "skill.json").write_text(_create_minimal_manifest("cleanup_pkg"), encoding="utf-8")
        (pkg_dir / "main.py").write_text("pass", encoding="utf-8")

        scratch_dir = tmp_path / "scratch"
        scratch_dir.mkdir()

        staged = stage_external_package(pkg_dir, scratch_dir, skills_root=skills_root)
        assert staged.staged_dir.exists()

        def _force_remove_readonly(func, path, exc_info):
            os.chmod(path, stat.S_IWRITE)
            func(path)

        import shutil
        shutil.rmtree(scratch_dir, onerror=_force_remove_readonly)
        assert not scratch_dir.exists()
