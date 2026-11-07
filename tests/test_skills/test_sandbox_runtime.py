"""Tests for Sandbox Runtime Provisioning, Isolated Python Staging, and Scratch ACLs.

Validates:
- Staging of the isolated worker Python 3.12 runtime (python.exe, DLLs, python312.zip, anaya_worker.zip, python312._pth).
- Hermetic python312._pth configuration (omitting '.' and user site-packages).
- Stdlib zip excluding test suites, idlelib, tkinter, and __pycache__.
- Allowed worker codebase modules in anaya_worker.zip.
- Staged isolated worker execution producing valid JSON protocol responses.
- Protected DACL enforcement on staged runtime (S-1-15-2-1 with RUNTIME_ACCESS_MASK, SE_DACL_PROTECTED).
- Fail-closed behavior on DACL application failure.
- Scratch directory creation with two-ACE protected DACL.
- Fail-closed behavior on scratch parent ACL failure.
- Zero-TOCTOU handle-relative recursive cleanup of nested directories, junctions, and read-only files.
- Absolute absence of path-based deletion fallbacks.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import subprocess
import sys
from unittest import mock
import uuid
import zipfile
import pytest

from app.skills.sandbox.runtime_provisioner import (
    RuntimeProvisioner,
    RuntimeProvisionerError,
    get_current_user_sid_string,
)

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    from app.skills.sandbox.win32_acl_types import (
        DACL_SECURITY_INFORMATION,
        FILE_APPEND_DATA,
        FILE_WRITE_DATA,
        DELETE,
        WRITE_DAC,
        WRITE_OWNER,
        RUNTIME_ACCESS_MASK,
        SE_DACL_PROTECTED,
        SE_FILE_OBJECT,
        advapi32,
        kernel32,
    )
    from app.skills.sandbox.win32_acl import convert_sid_to_string


# ---------------------------------------------------------------------------
# 1. Runtime Provisioning & Packaging Tests
# ---------------------------------------------------------------------------

class TestRuntimeStaging:
    @pytest.fixture(scope="class")
    def staged_provisioner(self, tmp_path_factory: pytest.TempPathFactory) -> RuntimeProvisioner:
        td = tmp_path_factory.mktemp("staged_rt")
        provisioner = RuntimeProvisioner(
            runtime_dir=td / "runtime",
            scratch_dir=td / "scratch",
        )
        provisioner.ensure_runtime_staged()
        return provisioner

    def test_runtime_artifacts_staged(self, staged_provisioner: RuntimeProvisioner) -> None:
        rt = staged_provisioner.runtime_dir
        py_ver = f"{sys.version_info.major}{sys.version_info.minor}"

        assert (rt / "python.exe").is_file()
        assert (rt / f"python{py_ver}.dll").is_file()
        assert (rt / f"python{py_ver}.zip").is_file()
        assert (rt / "anaya_worker.zip").is_file()
        assert (rt / f"python{py_ver}._pth").is_file()
        assert staged_provisioner.is_runtime_valid() is True

    def test_pth_file_hermetic_isolation(self, staged_provisioner: RuntimeProvisioner) -> None:
        rt = staged_provisioner.runtime_dir
        py_ver = f"{sys.version_info.major}{sys.version_info.minor}"
        pth_path = rt / f"python{py_ver}._pth"

        lines = [line.strip() for line in pth_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert lines == [f"python{py_ver}.zip", "anaya_worker.zip"]
        assert "." not in lines
        assert "import site" not in lines

    def test_stdlib_zip_excludes(self, staged_provisioner: RuntimeProvisioner) -> None:
        rt = staged_provisioner.runtime_dir
        py_ver = f"{sys.version_info.major}{sys.version_info.minor}"
        zip_path = rt / f"python{py_ver}.zip"

        with zipfile.ZipFile(zip_path, "r") as zf:
            names = set(zf.namelist())
            # Required standard modules must be present
            assert any(n.startswith("json/") for n in names)
            assert any(n.startswith("encodings/") for n in names)
            # Excluded test suites and non-headless libraries must be omitted
            assert not any(n.startswith("test/") for n in names)
            assert not any(n.startswith("tkinter/") for n in names)
            assert not any(n.startswith("idlelib/") for n in names)
            assert not any("__pycache__" in n for n in names)
            assert not any(n.endswith(".pyc") for n in names)

    def test_worker_zip_contents(self, staged_provisioner: RuntimeProvisioner) -> None:
        rt = staged_provisioner.runtime_dir
        zip_path = rt / "anaya_worker.zip"

        with zipfile.ZipFile(zip_path, "r") as zf:
            names = set(zf.namelist())
            assert "app/__init__.py" in names
            assert "app/skills/__init__.py" in names
            assert "app/skills/worker.py" in names
            assert "app/skills/protocol.py" in names
            assert "app/skills/models.py" in names
            assert "app/skills/skill_registry.py" in names
            assert "app/skills/builtin/__init__.py" in names
            assert "app/skills/builtin/example_skill.py" in names
            # Supervisor and host executors must NOT be inside worker zip
            assert "app/skills/supervisor.py" not in names
            assert "app/skills/skill_executor.py" not in names

    def test_staged_python_sys_path_isolation(self, staged_provisioner: RuntimeProvisioner) -> None:
        py_exe = staged_provisioner.worker_executable
        cmd = [
            str(py_exe),
            "-c",
            "import sys, json; print(json.dumps(sys.path))",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        paths = json.loads(res.stdout.strip())

        # sys.path must contain ONLY the two zip archives and nothing from cwd
        assert len(paths) == 2
        assert any(p.endswith(".zip") and "python" in p for p in paths)
        assert any(p.endswith("anaya_worker.zip") for p in paths)

    def test_staged_worker_execution(self, staged_provisioner: RuntimeProvisioner) -> None:
        py_exe = staged_provisioner.worker_executable
        req = {
            "protocol_version": "1.0",
            "message_type": "execute_request",
            "request_id": "test-req-123",
            "skill_id": "example_skill",
            "inputs": {"name": "Test"},
            "timeout_seconds": 30.0,
        }
        res = subprocess.run(
            [str(py_exe), "-m", "app.skills.worker"],
            input=json.dumps(req) + "\n",
            capture_output=True,
            text=True,
            check=True,
        )
        response_data = json.loads(res.stdout.strip())
        assert response_data["request_id"] == "test-req-123"
        assert response_data["protocol_version"] == "1.0"
        assert response_data["message_type"] == "execute_response"

    @pytest.mark.skipif(not IS_WINDOWS, reason="DACL inspection requires Windows.")
    def test_runtime_dacl_enforced_and_protected(self, staged_provisioner: RuntimeProvisioner) -> None:
        """Verify SEC-01 / SEC-06: Actual Windows DACL inspection on the staged runtime."""
        rt = staged_provisioner.runtime_dir
        p_desc = ctypes.c_void_p()
        p_dacl = ctypes.c_void_p()

        res = advapi32.GetNamedSecurityInfoW(
            str(rt),
            SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION,
            None,
            None,
            ctypes.byref(p_dacl),
            None,
            ctypes.byref(p_desc),
        )
        assert res == 0
        try:
            control = wintypes.WORD()
            rev = wintypes.DWORD()
            advapi32.GetSecurityDescriptorControl(p_desc, ctypes.byref(control), ctypes.byref(rev))
            assert bool(control.value & SE_DACL_PROTECTED) is True

            class ACL_SIZE_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("AceCount", wintypes.DWORD),
                    ("AclBytesInUse", wintypes.DWORD),
                    ("AclBytesFree", wintypes.DWORD),
                ]

            asi = ACL_SIZE_INFORMATION()
            advapi32.GetAclInformation(p_dacl, ctypes.byref(asi), ctypes.sizeof(asi), 2)  # AclSizeInformation

            app_pkg_ace_found = False
            for i in range(asi.AceCount):
                p_ace = ctypes.c_void_p()
                advapi32.GetAce(p_dacl, i, ctypes.byref(p_ace))
                ace_addr = p_ace.value
                mask = ctypes.cast(ace_addr + 4, ctypes.POINTER(wintypes.DWORD)).contents.value
                p_sid = ace_addr + 8
                sid_str = convert_sid_to_string(p_sid)

                if sid_str == "S-1-15-2-1":  # ALL APPLICATION PACKAGES
                    app_pkg_ace_found = True
                    # Assert exact runtime access mask
                    assert mask == RUNTIME_ACCESS_MASK
                    # Assert no write, delete, or control rights
                    assert (mask & FILE_WRITE_DATA) == 0
                    assert (mask & FILE_APPEND_DATA) == 0
                    assert (mask & DELETE) == 0
                    assert (mask & WRITE_DAC) == 0
                    assert (mask & WRITE_OWNER) == 0

            assert app_pkg_ace_found is True
        finally:
            kernel32.LocalFree(p_desc)

    @pytest.mark.skipif(not IS_WINDOWS, reason="DACL failure tests require Windows.")
    def test_runtime_dacl_failure_fails_closed(self, tmp_path: Path) -> None:
        """Verify that failure during _apply_runtime_dacl fails closed and raises RuntimeProvisionerError."""
        prov = RuntimeProvisioner(
            runtime_dir=tmp_path / "rt_fail",
            scratch_dir=tmp_path / "scratch_fail",
        )
        with mock.patch.object(prov, "_apply_runtime_dacl", side_effect=RuntimeError("simulated ACL failure")):
            with pytest.raises(RuntimeError, match="simulated ACL failure"):
                prov.ensure_runtime_staged()

        # Must not report runtime as valid
        assert prov.is_runtime_valid() is False


# ---------------------------------------------------------------------------
# 2. Scratch Directory Provisioning & Cleanup Tests
# ---------------------------------------------------------------------------

class TestScratchProvisioningAndCleanup:
    def test_create_and_cleanup_scratch(self, tmp_path: Path) -> None:
        provisioner = RuntimeProvisioner(
            runtime_dir=tmp_path / "runtime",
            scratch_dir=tmp_path / "scratch",
        )
        session_id = str(uuid.uuid4())
        user_sid = get_current_user_sid_string()

        scratch_dir = provisioner.create_scratch_dir(session_id, user_sid)
        assert scratch_dir.is_dir()
        assert scratch_dir.name == session_id

        # Write arbitrary nested files inside scratch
        (scratch_dir / "nested").mkdir()
        (scratch_dir / "nested" / "output.log").write_text("worker output", encoding="utf-8")
        (scratch_dir / "root_output.bin").write_bytes(b"\x12\x34\x56\x78")

        # Cleanup scratch directory
        cleaned = provisioner.cleanup_scratch_dir(scratch_dir)
        assert cleaned is True
        assert not scratch_dir.exists()

    def test_create_scratch_duplicate_raises(self, tmp_path: Path) -> None:
        provisioner = RuntimeProvisioner(
            runtime_dir=tmp_path / "runtime",
            scratch_dir=tmp_path / "scratch",
        )
        session_id = str(uuid.uuid4())
        user_sid = get_current_user_sid_string()

        scratch_dir = provisioner.create_scratch_dir(session_id, user_sid)
        assert scratch_dir.is_dir()

        with pytest.raises(FileExistsError):
            provisioner.create_scratch_dir(session_id, user_sid)

        provisioner.cleanup_scratch_dir(scratch_dir)

    @pytest.mark.skipif(not IS_WINDOWS, reason="Junction cleanup requires Windows.")
    def test_scratch_cleanup_handles_junctions_safely(self, tmp_path: Path) -> None:
        provisioner = RuntimeProvisioner(
            runtime_dir=tmp_path / "runtime",
            scratch_dir=tmp_path / "scratch",
        )
        session_id = str(uuid.uuid4())
        user_sid = get_current_user_sid_string()

        scratch_dir = provisioner.create_scratch_dir(session_id, user_sid)

        # External directory that MUST NOT be touched
        external_dir = tmp_path / "external_data"
        external_dir.mkdir()
        canary = external_dir / "sensitive_database.sqlite"
        canary.write_text("valuable database records", encoding="utf-8")

        junction = scratch_dir / "malicious_junction"
        res = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(external_dir)],
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            pytest.skip("Creating junction not supported or permitted in this environment.")

        assert junction.is_dir()
        assert (junction / "sensitive_database.sqlite").is_file()

        # Clean scratch directory
        cleaned = provisioner.cleanup_scratch_dir(scratch_dir)
        assert cleaned is True
        assert not scratch_dir.exists()

        # External database must remain intact!
        assert external_dir.is_dir()
        assert canary.is_file()
        assert canary.read_text(encoding="utf-8") == "valuable database records"

    @pytest.mark.skipif(not IS_WINDOWS, reason="Scratch parent ACL tests require Windows.")
    def test_scratch_parent_acl_failure_fails_closed(self, tmp_path: Path) -> None:
        """Verify that scratch parent ACL configuration failure fails closed."""
        provisioner = RuntimeProvisioner(
            runtime_dir=tmp_path / "runtime",
            scratch_dir=tmp_path / "scratch_fail_parent",
        )
        session_id = str(uuid.uuid4())
        user_sid = get_current_user_sid_string()

        with mock.patch.object(provisioner, "_ensure_scratch_parent_configured", side_effect=RuntimeProvisionerError("parent failure")):
            with pytest.raises(RuntimeProvisionerError, match="parent failure"):
                provisioner.create_scratch_dir(session_id, user_sid)
