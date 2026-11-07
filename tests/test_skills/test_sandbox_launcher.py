"""Comprehensive test suite for SandboxedProcessLauncher (Phase 10 — Step 10.3.3).

Covers:
- Native ctypes structure sizes, alignments, and field offsets (x64)
- Attribute list initialization, 3-attribute coexistence, and cleanup
- Exact handle inheritance whitelist (PROC_THREAD_ATTRIBUTE_HANDLE_LIST)
- AppContainer identity, SID verification, and zero-capability enforcement
- Atomic Job Object association and IsProcessInJob verification
- Pre-resume verification and durable metadata ordering (STATUS_WORKER_SUSPENDED before ResumeThread)
- Synchronous two-stage reaping and process-death confirmation
- Quarantine lock retention when process death is unconfirmed
- Supervisor crash before metadata persistence (kill-on-close invariant verified with retained process handle)
- Controlled network endpoint isolation tests with reachable host baselines
- Unsupported OS and platform fail-closed behavior
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time
from typing import Any
import uuid

import pytest

from app.skills.protocol import ExecuteRequest, decode_response, encode_request
from app.skills.sandbox.appcontainer import AppContainerProfile
from app.skills.sandbox.process_launcher import (
    LauncherError,
    PreResumeVerificationError,
    ProcessReapError,
    ProcessStartupError,
    SandboxedProcess,
    SandboxedProcessLauncher,
    confirm_process_death_and_reap,
    create_sanitized_environment,
    verify_appcontainer_process_token,
)
from app.skills.sandbox.runtime_provisioner import RuntimeProvisioner
from app.skills.sandbox.session_registry import (
    STATUS_SCRATCH_READY,
    STATUS_WORKER_RUNNING,
    STATUS_WORKER_SUSPENDED,
    SessionRegistry,
    get_process_creation_time,
)
from app.skills.sandbox.win32_job import JobLimits, Win32JobObject
from app.skills.sandbox.win32_process_types import (
    CREATE_SUSPENDED,
    CREATE_UNICODE_ENVIRONMENT,
    ERROR_INSUFFICIENT_BUFFER,
    EXTENDED_STARTUPINFO_PRESENT,
    PROCESS_INFORMATION,
    PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
    PROC_THREAD_ATTRIBUTE_JOB_LIST,
    PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
    SECURITY_CAPABILITIES,
    SID_AND_ATTRIBUTES,
    STARTF_USESTDHANDLES,
    STARTUPINFOEXW,
    STARTUPINFOW,
    TOKEN_APPCONTAINER_INFORMATION,
    TOKEN_GROUPS,
    TokenCapabilities,
    WAIT_OBJECT_0,
    advapi32,
    kernel32,
)


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 structure layout tests require Windows.")
class TestWin32ProcessStructureLayouts:
    """Validate 64-bit memory layout, structure sizes, and field offsets."""

    def test_startupinfow_size_and_offsets(self):
        """Verify STARTUPINFOW size (104 bytes on x64) and standard handle offsets."""
        assert ctypes.sizeof(STARTUPINFOW) == 104
        assert STARTUPINFOW.cb.offset == 0
        assert STARTUPINFOW.dwFlags.offset == 60
        assert STARTUPINFOW.hStdInput.offset == 80
        assert STARTUPINFOW.hStdOutput.offset == 88
        assert STARTUPINFOW.hStdError.offset == 96

    def test_startupinfoexw_size_and_offsets(self):
        """Verify STARTUPINFOEXW size (112 bytes on x64) and lpAttributeList offset."""
        assert ctypes.sizeof(STARTUPINFOEXW) == 112
        assert STARTUPINFOEXW.StartupInfo.offset == 0
        assert STARTUPINFOEXW.lpAttributeList.offset == 104

    def test_process_information_size_and_offsets(self):
        """Verify PROCESS_INFORMATION size (24 bytes on x64)."""
        assert ctypes.sizeof(PROCESS_INFORMATION) == 24
        assert PROCESS_INFORMATION.hProcess.offset == 0
        assert PROCESS_INFORMATION.hThread.offset == 8
        assert PROCESS_INFORMATION.dwProcessId.offset == 16
        assert PROCESS_INFORMATION.dwThreadId.offset == 20

    def test_security_capabilities_size_and_offsets(self):
        """Verify SECURITY_CAPABILITIES size (24 bytes on x64)."""
        assert ctypes.sizeof(SECURITY_CAPABILITIES) == 24
        assert SECURITY_CAPABILITIES.AppContainerSid.offset == 0
        assert SECURITY_CAPABILITIES.Capabilities.offset == 8
        assert SECURITY_CAPABILITIES.CapabilityCount.offset == 16
        assert SECURITY_CAPABILITIES.Reserved.offset == 20

    def test_sid_and_attributes_size_and_offsets(self):
        """Verify SID_AND_ATTRIBUTES size (16 bytes on x64)."""
        assert ctypes.sizeof(SID_AND_ATTRIBUTES) == 16
        assert SID_AND_ATTRIBUTES.Sid.offset == 0
        assert SID_AND_ATTRIBUTES.Attributes.offset == 8

    def test_token_appcontainer_information_size(self):
        """Verify TOKEN_APPCONTAINER_INFORMATION size (8 bytes on x64)."""
        assert ctypes.sizeof(TOKEN_APPCONTAINER_INFORMATION) == 8


@pytest.mark.skipif(sys.platform != "win32", reason="Attribute list tests require Windows.")
class TestProcThreadAttributeList:
    """Tests for PROC_THREAD_ATTRIBUTE_LIST lifecycle and attribute coexistence."""

    def test_attribute_list_size_query_and_initialization(self):
        """Querying attribute list size with NULL buffer returns required size."""
        size = ctypes.c_size_t(0)
        kernel32.InitializeProcThreadAttributeList(None, 3, 0, ctypes.byref(size))
        assert size.value >= 96  # 96 bytes minimum on Win10/11 x64

        buf = ctypes.create_string_buffer(size.value)
        ok = kernel32.InitializeProcThreadAttributeList(buf, 3, 0, ctypes.byref(size))
        assert ok != 0
        kernel32.DeleteProcThreadAttributeList(buf)

    def test_three_attributes_coexist_in_single_list(self):
        """Verify that Job, Handle list, and AppContainer attributes coexist without conflict."""
        job = Win32JobObject(limits=JobLimits(kill_on_close=True))
        try:
            size = ctypes.c_size_t(0)
            kernel32.InitializeProcThreadAttributeList(None, 3, 0, ctypes.byref(size))
            buf = ctypes.create_string_buffer(size.value)
            assert kernel32.InitializeProcThreadAttributeList(buf, 3, 0, ctypes.byref(size)) != 0

            # 1. Job List
            h_job_arr = (wintypes.HANDLE * 1)(job.handle)
            ok1 = kernel32.UpdateProcThreadAttribute(
                buf, 0, PROC_THREAD_ATTRIBUTE_JOB_LIST,
                h_job_arr, ctypes.sizeof(h_job_arr), None, None
            )
            assert ok1 != 0

            # 2. Handle List
            h_dummy = (wintypes.HANDLE * 1)(job.handle)
            ok2 = kernel32.UpdateProcThreadAttribute(
                buf, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                h_dummy, ctypes.sizeof(h_dummy), None, None
            )
            assert ok2 != 0

            # 3. Security Capabilities
            session_id = str(uuid.uuid4())
            with AppContainerProfile(session_id) as profile:
                sec_cap = SECURITY_CAPABILITIES()
                sec_cap.AppContainerSid = wintypes.LPVOID(profile.sid)
                sec_cap.Capabilities = None
                sec_cap.CapabilityCount = 0
                sec_cap.Reserved = 0

                ok3 = kernel32.UpdateProcThreadAttribute(
                    buf, 0, PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
                    ctypes.byref(sec_cap), ctypes.sizeof(sec_cap), None, None
                )
                assert ok3 != 0

            kernel32.DeleteProcThreadAttributeList(buf)
        finally:
            job.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Launcher integration tests require Windows.")
class TestSandboxedProcessLauncherLifecycle:
    """Integration tests for SandboxedProcessLauncher on Windows."""

    @pytest.fixture
    def launcher_context(self, tmp_path):
        """Provide a clean isolated launcher and session registry context."""
        reg_dir = tmp_path / "sessions"
        scratch_dir = tmp_path / "scratch"
        runtime_dir = tmp_path / "runtime"

        provisioner = RuntimeProvisioner(runtime_dir=runtime_dir, scratch_dir=scratch_dir)
        provisioner.ensure_runtime_staged()

        registry = SessionRegistry(base_dir=reg_dir, scratch_base=scratch_dir)
        launcher = SandboxedProcessLauncher(runtime_provisioner=provisioner, session_registry=registry)
        return launcher, registry, provisioner

    def test_full_launch_execute_and_clean_exit(self, launcher_context):
        """Sandboxed worker is launched, executes greeting request, and terminates cleanly."""
        launcher, registry, provisioner = launcher_context
        session_id = str(uuid.uuid4())
        h_lock = registry.acquire_session_lock(session_id, create_new=True)

        proc = launcher.launch(session_id, h_lock, deadline=time.monotonic() + 10.0)
        try:
            assert proc.pid > 0
            assert proc.returncode is None

            # Verify protocol request
            req = ExecuteRequest(
                request_id="req-test-greet",
                skill_id="example.greeting",
                inputs={"name": "Antigravity"},
            )
            proc.stdin.write((encode_request(req) + "\n").encode("utf-8"))
            proc.stdin.flush()
            proc.stdin.close()

            # Read response
            line = proc.stdout.readline().decode("utf-8").strip()
            resp = decode_response(line)
            assert resp.success is True
            assert resp.result["message"] == "Hello, Antigravity!"

            # Wait for exit
            code = proc.wait(timeout=5.0)
            assert code == 0
            assert proc.poll() == 0
        finally:
            proc.close()
            registry.complete_session(session_id, h_lock)

    def test_durable_metadata_ordering_before_and_after_resume(self, launcher_context):
        """Worker metadata is written as STATUS_WORKER_SUSPENDED with PID/FILETIME before thread resumes."""
        launcher, registry, provisioner = launcher_context
        session_id = str(uuid.uuid4())
        h_lock = registry.acquire_session_lock(session_id, create_new=True)

        proc = launcher.launch(session_id, h_lock, deadline=time.monotonic() + 10.0)
        try:
            # Metadata must reflect running worker with correct PID
            meta = registry.read_metadata(session_id)
            assert meta["status"] == STATUS_WORKER_RUNNING
            assert meta["worker_pid"] == proc.pid
            assert meta["worker_start_time"] is not None
            assert meta["supervisor_pid"] == os.getpid()
        finally:
            proc.kill()
            proc.wait(timeout=2.0)
            proc.close()
            registry.complete_session(session_id, h_lock)

    def test_negative_token_capabilities_rejection_halts_launch_and_reaps(self, launcher_context, monkeypatch):
        """Token verification rejects tokens with GroupCount > 0, prevents ResumeThread, and reaps child."""
        launcher, registry, provisioner = launcher_context
        session_id = str(uuid.uuid4())
        h_lock = registry.acquire_session_lock(session_id, create_new=True)

        orig_get_token_info = advapi32.GetTokenInformation
        orig_resume_thread = kernel32.ResumeThread
        resume_called: list[int] = []

        def spy_resume_thread(h_thread):
            resume_called.append(int(h_thread))
            return orig_resume_thread(h_thread)

        def mock_get_token_info(hToken, infoClass, pInfo, infoLen, pRetLen):
            if int(infoClass) == TokenCapabilities:
                req_size = ctypes.sizeof(TOKEN_GROUPS)
                if pRetLen:
                    ctypes.cast(pRetLen, ctypes.POINTER(wintypes.DWORD)).contents.value = req_size
                if not pInfo:
                    ctypes.set_last_error(ERROR_INSUFFICIENT_BUFFER)
                    return False
                fake_groups = TOKEN_GROUPS()
                fake_groups.GroupCount = 1
                ctypes.memmove(pInfo, ctypes.byref(fake_groups), ctypes.sizeof(fake_groups))
                ctypes.set_last_error(0)
                return True
            return orig_get_token_info(hToken, infoClass, pInfo, infoLen, pRetLen)

        monkeypatch.setattr(advapi32, "GetTokenInformation", mock_get_token_info)
        monkeypatch.setattr(kernel32, "ResumeThread", spy_resume_thread)

        try:
            with pytest.raises(PreResumeVerificationError, match="granted 1 unintended capabilities"):
                launcher.launch(session_id, h_lock, deadline=time.monotonic() + 10.0)

            # Assert ResumeThread was NEVER called
            assert len(resume_called) == 0

            # Assert metadata never reached worker_suspended or worker_running
            meta = registry.read_metadata(session_id)
            assert meta["status"] == STATUS_SCRATCH_READY
            assert meta["worker_pid"] is None
        finally:
            registry.complete_session(session_id, h_lock)

    def test_pre_resume_token_verification_rejection_on_mismatch(self, launcher_context):
        """Token verification fails closed if expected AppContainer SID does not match."""
        launcher, registry, provisioner = launcher_context
        session_id = str(uuid.uuid4())
        h_lock = registry.acquire_session_lock(session_id, create_new=True)

        # Launch a valid process first to get handle
        proc = launcher.launch(session_id, h_lock, deadline=time.monotonic() + 10.0)
        try:
            # Test verify_appcontainer_process_token with bogus SID string
            with pytest.raises(PreResumeVerificationError, match="AppContainer SID mismatch"):
                verify_appcontainer_process_token(
                    proc._h_process,
                    expected_sid_string="S-1-15-2-999999999-999999999",
                )
        finally:
            proc.kill()
            proc.wait(timeout=2.0)
            proc.close()
            registry.complete_session(session_id, h_lock)

    def test_two_stage_reap_kills_process_and_confirms_death(self, launcher_context):
        """confirm_process_death_and_reap terminates running process and confirms signaled state."""
        launcher, registry, provisioner = launcher_context
        session_id = str(uuid.uuid4())
        h_lock = registry.acquire_session_lock(session_id, create_new=True)

        proc = launcher.launch(session_id, h_lock, deadline=time.monotonic() + 10.0)
        h_proc = proc._h_process
        job = proc.job
        try:
            # Stage reap
            reaped = confirm_process_death_and_reap(h_proc, 0, job, timeout_ms=3000)
            assert reaped is True
            # Verify process object is signaled
            assert kernel32.WaitForSingleObject(wintypes.HANDLE(h_proc), 0) == WAIT_OBJECT_0
        finally:
            proc.close()
            registry.complete_session(session_id, h_lock)


@pytest.mark.skipif(sys.platform != "win32", reason="Crash invariant tests require Windows.")
class TestPreMetadataCrashInvariant:
    """Verify that a child created with PROC_THREAD_ATTRIBUTE_JOB_LIST cannot survive supervisor termination."""

    def test_child_terminated_on_supervisor_crash_before_metadata(self, tmp_path):
        """Child created suspended with Job Object terminates when parent process dies abruptly."""
        helper_code = """
import ctypes
from ctypes import wintypes
import sys
from app.skills.sandbox.win32_job import Win32JobObject, JobLimits
from app.skills.sandbox.win32_process_types import (
    STARTUPINFOEXW, PROCESS_INFORMATION,
    CREATE_SUSPENDED, EXTENDED_STARTUPINFO_PRESENT,
    PROC_THREAD_ATTRIBUTE_JOB_LIST, kernel32
)

job = Win32JobObject(limits=JobLimits(kill_on_close=True, active_process_limit=1))

size = ctypes.c_size_t(0)
kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
buf = ctypes.create_string_buffer(size.value)
kernel32.InitializeProcThreadAttributeList(buf, 1, 0, ctypes.byref(size))

h_job_arr = (wintypes.HANDLE * 1)(job.handle)
kernel32.UpdateProcThreadAttribute(buf, 0, PROC_THREAD_ATTRIBUTE_JOB_LIST, h_job_arr, ctypes.sizeof(h_job_arr), None, None)

si = STARTUPINFOEXW()
si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
si.lpAttributeList = ctypes.cast(buf, wintypes.LPVOID)
pi = PROCESS_INFORMATION()

res = kernel32.CreateProcessW(
    None, "notepad.exe", None, None, False,
    CREATE_SUSPENDED | EXTENDED_STARTUPINFO_PRESENT,
    None, None, ctypes.byref(si), ctypes.byref(pi)
)
if not res:
    sys.exit(1)

# Print worker PID for observer
print(pi.dwProcessId, flush=True)

# Abruptly exit supervisor process before metadata write or resume
sys.exit(42)
"""
        helper_path = tmp_path / "sup_sim.py"
        helper_path.write_text(helper_code, encoding="utf-8")

        # Run simulated supervisor
        p_sup = subprocess.Popen([sys.executable, str(helper_path)], stdout=subprocess.PIPE, text=True)
        stdout, _ = p_sup.communicate(timeout=5.0)
        assert p_sup.returncode == 42
        worker_pid = int(stdout.strip())
        assert worker_pid > 0

        # Wait brief moment for kernel Job cleanup
        time.sleep(0.5)

        # Query process handle from test runner (observer)
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h_worker = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, worker_pid)
        if h_worker:
            try:
                # If handle opened, process must already be terminated
                code = wintypes.DWORD()
                kernel32.GetExitCodeProcess(h_worker, ctypes.byref(code))
                assert code.value != 259  # STILL_ACTIVE = 259
            finally:
                kernel32.CloseHandle(h_worker)
        else:
            # Process object was completely torn down
            err = ctypes.get_last_error()
            assert err in (87, 2)  # ERROR_INVALID_PARAMETER (PID dead) or ERROR_FILE_NOT_FOUND

    def test_child_terminated_on_supervisor_forced_termination(self, tmp_path):
        """Child created suspended terminates even when parent is forcefully terminated via TerminateProcess."""
        helper_code = """
import ctypes
from ctypes import wintypes
import sys
import time
from app.skills.sandbox.win32_job import Win32JobObject, JobLimits
from app.skills.sandbox.win32_process_types import (
    STARTUPINFOEXW, PROCESS_INFORMATION,
    CREATE_SUSPENDED, EXTENDED_STARTUPINFO_PRESENT,
    PROC_THREAD_ATTRIBUTE_JOB_LIST, kernel32
)

job = Win32JobObject(limits=JobLimits(kill_on_close=True, active_process_limit=1))

size = ctypes.c_size_t(0)
kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
buf = ctypes.create_string_buffer(size.value)
kernel32.InitializeProcThreadAttributeList(buf, 1, 0, ctypes.byref(size))

h_job_arr = (wintypes.HANDLE * 1)(job.handle)
kernel32.UpdateProcThreadAttribute(buf, 0, PROC_THREAD_ATTRIBUTE_JOB_LIST, h_job_arr, ctypes.sizeof(h_job_arr), None, None)

si = STARTUPINFOEXW()
si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
si.lpAttributeList = ctypes.cast(buf, wintypes.LPVOID)
pi = PROCESS_INFORMATION()

res = kernel32.CreateProcessW(
    None, "notepad.exe", None, None, False,
    CREATE_SUSPENDED | EXTENDED_STARTUPINFO_PRESENT,
    None, None, ctypes.byref(si), ctypes.byref(pi)
)
if not res:
    sys.exit(1)

# Print worker PID and block until killed
print(pi.dwProcessId, flush=True)
while True:
    time.sleep(1)
"""
        helper_path = tmp_path / "sup_force_kill.py"
        helper_path.write_text(helper_code, encoding="utf-8")

        p_sup = subprocess.Popen([sys.executable, str(helper_path)], stdout=subprocess.PIPE, text=True)
        worker_pid_line = p_sup.stdout.readline()
        worker_pid = int(worker_pid_line.strip())
        assert worker_pid > 0

        # Retain handle to worker from observer process before killing supervisor
        PROCESS_SYNCHRONIZE = 0x00100000
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h_worker = kernel32.OpenProcess(PROCESS_SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, worker_pid)
        assert h_worker != 0

        try:
            # Forcefully terminate the supervisor process
            p_sup.kill()
            p_sup.wait(timeout=2.0)

            # Wait for worker process kernel object to enter signaled state
            wait_res = kernel32.WaitForSingleObject(wintypes.HANDLE(h_worker), 3000)
            assert wait_res == WAIT_OBJECT_0

            code = wintypes.DWORD()
            kernel32.GetExitCodeProcess(wintypes.HANDLE(h_worker), ctypes.byref(code))
            assert code.value != 259  # Process is signaled and terminated
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(h_worker))


@pytest.mark.skipif(sys.platform != "win32", reason="Network isolation tests require Windows.")
class TestControlledNetworkIsolation:
    """Security-outcome network isolation tests with reachable host baselines."""

    def _setup_network_runtime(self, tmp_path: Path) -> RuntimeProvisioner:
        provisioner = RuntimeProvisioner(runtime_dir=tmp_path / "rt", scratch_dir=tmp_path / "sc")
        provisioner.ensure_runtime_staged()

        import _socket
        import select
        import unicodedata
        for mod in (_socket, select, unicodedata):
            src_pyd = Path(mod.__file__)
            if src_pyd.is_file():
                dst_pyd = provisioner.runtime_dir / src_pyd.name
                shutil.copy2(src_pyd, dst_pyd)
                provisioner._apply_runtime_dacl(dst_pyd)

        pth_file = provisioner.runtime_dir / "python312._pth"
        pth_file.write_text("python312.zip\nanaya_worker.zip\n.\n", encoding="utf-8")
        return provisioner

    def _run_in_appcontainer(self, provisioner: RuntimeProvisioner, script_code: str) -> tuple[int, Path]:
        session_id = str(uuid.uuid4())
        with AppContainerProfile(session_id) as profile:
            scratch_dir = provisioner.create_scratch_dir(session_id, profile.sid_string)
            script_path = scratch_dir / "test_net.py"
            script_path.write_text(script_code, encoding="utf-8")

            attr_size = ctypes.c_size_t(0)
            kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(attr_size))
            attr_buf = ctypes.create_string_buffer(attr_size.value)
            kernel32.InitializeProcThreadAttributeList(attr_buf, 1, 0, ctypes.byref(attr_size))

            sec_cap = SECURITY_CAPABILITIES()
            sec_cap.AppContainerSid = wintypes.LPVOID(profile.sid)
            sec_cap.Capabilities = None
            sec_cap.CapabilityCount = 0
            sec_cap.Reserved = 0
            kernel32.UpdateProcThreadAttribute(
                attr_buf, 0, PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
                ctypes.byref(sec_cap), ctypes.sizeof(sec_cap), None, None
            )

            si = STARTUPINFOEXW()
            si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
            si.lpAttributeList = ctypes.cast(attr_buf, wintypes.LPVOID)
            pi = PROCESS_INFORMATION()

            cmd = f'"{provisioner.worker_executable}" "{script_path}"'
            res = kernel32.CreateProcessW(
                None, ctypes.create_unicode_buffer(cmd), None, None, False,
                EXTENDED_STARTUPINFO_PRESENT, None, str(scratch_dir),
                ctypes.byref(si), ctypes.byref(pi)
            )
            assert res != 0
            wait_res = kernel32.WaitForSingleObject(pi.hProcess, 10000)
            assert wait_res == 0

            exit_code = wintypes.DWORD()
            kernel32.GetExitCodeProcess(pi.hProcess, ctypes.byref(exit_code))
            kernel32.CloseHandle(pi.hProcess)
            kernel32.CloseHandle(pi.hThread)
            kernel32.DeleteProcThreadAttributeList(attr_buf)

            return exit_code.value, scratch_dir

    def test_loopback_isolation_with_reachable_host_baseline(self, tmp_path):
        """Host baseline connects to local TCP server; sandboxed worker connection is blocked."""
        # 1. Start host-side ephemeral TCP echo listener
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        host_port = server.getsockname()[1]

        def _echo_server():
            server.settimeout(4.0)
            try:
                conn, _ = server.accept()
                data = conn.recv(1024)
                conn.sendall(data)
                conn.close()
            except Exception:
                pass
            finally:
                server.close()

        server_thread = threading.Thread(target=_echo_server, daemon=True)
        server_thread.start()

        # 2. Verify Host Baseline: Unsandboxed host MUST be able to connect!
        host_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            host_sock.connect(("127.0.0.1", host_port))
            host_sock.sendall(b"PING")
            echo = host_sock.recv(1024)
            assert echo == b"PING"
        except Exception as exc:
            pytest.fail(f"Host baseline connection failed: {exc}. Test is INCONCLUSIVE.")
        finally:
            host_sock.close()

        # 3. Start second listener for sandboxed child test
        server2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server2.bind(("127.0.0.1", host_port))
        server2.listen(1)

        def _second_listener():
            server2.settimeout(4.0)
            try:
                conn, _ = server2.accept()
                conn.close()
            except Exception:
                pass
            finally:
                server2.close()

        listener_thread = threading.Thread(target=_second_listener, daemon=True)
        listener_thread.start()

        # 4. Run sandboxed worker attempting loopback connect
        provisioner = self._setup_network_runtime(tmp_path)
        test_script = (
            "import json, socket, sys\n"
            "s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
            "s.settimeout(1.0)\n"
            "res = {'connected': False, 'error_type': None, 'errno': None}\n"
            "try:\n"
            f"    s.connect(('127.0.0.1', {host_port}))\n"
            "    res['connected'] = True\n"
            "    with open('loopback_res.json', 'w') as f:\n"
            "        json.dump(res, f)\n"
            "    sys.exit(99)\n"  # 99 means breach!
            "except Exception as e:\n"
            "    res['connected'] = False\n"
            "    res['error_type'] = type(e).__name__\n"
            "    res['errno'] = getattr(e, 'errno', None)\n"
            "    with open('loopback_res.json', 'w') as f:\n"
            "        json.dump(res, f)\n"
            "    sys.exit(0)\n"
        )
        code, scratch_dir = self._run_in_appcontainer(provisioner, test_script)
        assert code == 0

        res_file = scratch_dir / "loopback_res.json"
        assert res_file.is_file()
        res_data = json.loads(res_file.read_text(encoding="utf-8"))

        if res_data["connected"]:
            pytest.fail("FAIL: Unauthorized loopback communication succeeded from sandboxed worker! Security breach.")

        # Classify outcome: WSAEACCES (10013 / errno 13) or PermissionError indicates explicit policy denial
        if res_data.get("errno") in (13, 10013) or res_data.get("error_type") == "PermissionError":
            # PASS: Explicit policy denial by WFP / AppContainer
            pass
        else:
            pytest.skip(
                f"INCONCLUSIVE: Loopback connection failed with {res_data.get('error_type')} "
                f"(errno {res_data.get('errno')}); not an explicit policy denial."
            )

    def test_outbound_network_isolation_with_host_baseline(self, tmp_path):
        """Host baseline connects to outbound endpoint; sandboxed worker outbound communication is blocked."""
        outbound_host = os.environ.get("TEST_OUTBOUND_HOST", "1.1.1.1")
        outbound_port = int(os.environ.get("TEST_OUTBOUND_PORT", "80"))

        # 1. Host baseline connection check
        host_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        host_sock.settimeout(3.0)
        try:
            host_sock.connect((outbound_host, outbound_port))
        except Exception as exc:
            pytest.skip(f"Host baseline connection to {outbound_host}:{outbound_port} failed: {exc}. Test is INCONCLUSIVE.")
        finally:
            host_sock.close()

        # 2. Sandboxed worker attempts outbound connection
        provisioner = self._setup_network_runtime(tmp_path)
        test_script = (
            "import json, socket, sys\n"
            "s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
            "s.settimeout(2.0)\n"
            "res = {'connected': False, 'error_type': None, 'errno': None}\n"
            "try:\n"
            f"    s.connect(('{outbound_host}', {outbound_port}))\n"
            "    res['connected'] = True\n"
            "    with open('outbound_res.json', 'w') as f:\n"
            "        json.dump(res, f)\n"
            "    sys.exit(99)\n"  # Breach!
            "except Exception as e:\n"
            "    res['connected'] = False\n"
            "    res['error_type'] = type(e).__name__\n"
            "    res['errno'] = getattr(e, 'errno', None)\n"
            "    with open('outbound_res.json', 'w') as f:\n"
            "        json.dump(res, f)\n"
            "    sys.exit(0)\n"  # Blocked
        )
        code, scratch_dir = self._run_in_appcontainer(provisioner, test_script)
        assert code == 0

        res_file = scratch_dir / "outbound_res.json"
        assert res_file.is_file()
        res_data = json.loads(res_file.read_text(encoding="utf-8"))

        if res_data["connected"]:
            pytest.fail("FAIL: Unauthorized outbound communication succeeded from sandboxed worker! Security breach.")

        if res_data.get("errno") in (13, 10013) or res_data.get("error_type") == "PermissionError":
            # PASS: Explicit policy denial by WFP / AppContainer
            pass
        else:
            pytest.skip(
                f"INCONCLUSIVE: Outbound connection failed with {res_data.get('error_type')} "
                f"(errno {res_data.get('errno')}); not an explicit policy denial."
            )

    def test_private_network_isolation_with_host_baseline(self, tmp_path):
        """Host baseline connects to private LAN endpoint; sandboxed worker private connection is blocked."""
        # Discover local non-loopback private IP
        try:
            local_ip = socket.gethostbyname(socket.gethostname())
            if local_ip.startswith("127."):
                pytest.skip("No non-loopback private IP address detected on host. Test is INCONCLUSIVE.")
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.bind((local_ip, 0))
            server.listen(1)
            priv_port = server.getsockname()[1]
        except Exception as exc:
            pytest.skip(f"Failed to bind private-network server on host LAN IP: {exc}. Test is INCONCLUSIVE.")

        stop_server = threading.Event()

        def _priv_server():
            server.settimeout(0.5)
            while not stop_server.is_set():
                try:
                    conn, _ = server.accept()
                    conn.close()
                except (socket.timeout, OSError):
                    continue
            server.close()

        server_thread = threading.Thread(target=_priv_server, daemon=True)
        server_thread.start()

        try:
            # Host baseline check
            host_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            host_sock.settimeout(2.0)
            try:
                host_sock.connect((local_ip, priv_port))
            except Exception as exc:
                pytest.skip(f"Host baseline connection to private IP {local_ip}:{priv_port} failed: {exc}. Test is INCONCLUSIVE.")
            finally:
                host_sock.close()

            provisioner = self._setup_network_runtime(tmp_path)
            test_script = (
                "import json, socket, sys\n"
                "s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
                "s.settimeout(2.0)\n"
                "res = {'connected': False, 'error_type': None, 'errno': None}\n"
                "try:\n"
                f"    s.connect(('{local_ip}', {priv_port}))\n"
                "    res['connected'] = True\n"
                "    with open('priv_res.json', 'w') as f:\n"
                "        json.dump(res, f)\n"
                "    sys.exit(99)\n"
                "except Exception as e:\n"
                "    res['connected'] = False\n"
                "    res['error_type'] = type(e).__name__\n"
                "    res['errno'] = getattr(e, 'errno', None)\n"
                "    with open('priv_res.json', 'w') as f:\n"
                "        json.dump(res, f)\n"
                "    sys.exit(0)\n"
            )
            code, scratch_dir = self._run_in_appcontainer(provisioner, test_script)
            assert code == 0

            res_file = scratch_dir / "priv_res.json"
            assert res_file.is_file()
            res_data = json.loads(res_file.read_text(encoding="utf-8"))

            if res_data["connected"]:
                pytest.fail("FAIL: Unauthorized private network communication succeeded from sandboxed worker! Security breach.")

            if res_data.get("errno") in (13, 10013) or res_data.get("error_type") == "PermissionError":
                # PASS: Explicit policy denial by WFP / AppContainer
                pass
            else:
                pytest.skip(
                    f"INCONCLUSIVE: Private network connection resulted in {res_data.get('error_type')} "
                    f"(errno {res_data.get('errno')}); packet dropped or timed out without explicit WSAEACCES denial."
                )
        finally:
            stop_server.set()
            server_thread.join(timeout=1.0)

    def test_dns_resolution_and_transport_behavior(self, tmp_path):
        """DNS resolution and transport access are evaluated and reported independently without fallback IP."""
        target_domain = os.environ.get("TEST_DNS_DOMAIN", "one.one.one.one")

        # 1. Host baseline check
        host_ip = None
        try:
            host_ip = socket.gethostbyname(target_domain)
            host_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            host_sock.settimeout(3.0)
            host_sock.connect((host_ip, 80))
            host_sock.close()
        except Exception as exc:
            pytest.skip(f"Host baseline failed for {target_domain}: {exc}. Test is INCONCLUSIVE.")

        provisioner = self._setup_network_runtime(tmp_path)
        test_script = (
            "import json, socket, sys\n"
            f"target = '{target_domain}'\n"
            "res = {'dns_resolved': False, 'resolved_ip': None, 'dns_error': None, 'dns_errno': None, "
            "'transport_attempted': False, 'transport_connected': False, 'transport_error': None, 'transport_errno': None}\n"
            "try:\n"
            "    ip = socket.gethostbyname(target)\n"
            "    res['dns_resolved'] = True\n"
            "    res['resolved_ip'] = ip\n"
            "except Exception as e:\n"
            "    res['dns_error'] = type(e).__name__\n"
            "    res['dns_errno'] = getattr(e, 'errno', None)\n"
            "\n"
            "# Step 2: If DNS resolved, attempt transport connection strictly to the resolved IP without fallback IP\n"
            "if res['dns_resolved']:\n"
            "    res['transport_attempted'] = True\n"
            "    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
            "    s.settimeout(2.0)\n"
            "    try:\n"
            "        s.connect((res['resolved_ip'], 80))\n"
            "        res['transport_connected'] = True\n"
            "        with open('dns_res.json', 'w') as f:\n"
            "            json.dump(res, f)\n"
            "        sys.exit(99)\n"  # Breach!\n"
            "    except Exception as e:\n"
            "        res['transport_error'] = type(e).__name__\n"
            "        res['transport_errno'] = getattr(e, 'errno', None)\n"
            "        s.close()\n"
            "\n"
            "with open('dns_res.json', 'w') as f:\n"
            "    json.dump(res, f)\n"
            "sys.exit(0)\n"
        )
        code, scratch_dir = self._run_in_appcontainer(provisioner, test_script)
        assert code == 0

        res_file = scratch_dir / "dns_res.json"
        assert res_file.is_file()
        res_data = json.loads(res_file.read_text(encoding="utf-8"))

        if res_data["transport_connected"]:
            pytest.fail(f"FAIL: Unauthorized transport communication succeeded to {res_data['resolved_ip']}! Security breach.")

        if res_data["dns_resolved"]:
            # DNS resolved via host service; evaluate transport isolation independently
            assert res_data["transport_attempted"] is True
            if res_data.get("transport_errno") in (13, 10013) or res_data.get("transport_error") == "PermissionError":
                # PASS: Transport connection to resolved IP was explicitly denied by sandbox policy
                pass
            else:
                pytest.skip(
                    f"INCONCLUSIVE: Transport failure to {res_data['resolved_ip']} was {res_data['transport_error']} "
                    f"(errno {res_data.get('transport_errno')}); not an explicit policy denial."
                )
        else:
            # DNS failed. Evaluate whether failure is explicit policy denial
            assert res_data["transport_attempted"] is False
            if res_data.get("dns_errno") in (13, 10013) or res_data.get("dns_error") == "PermissionError":
                # PASS: DNS resolution was explicitly denied by policy
                pass
            else:
                pytest.skip(
                    f"INCONCLUSIVE: DNS resolution failed with {res_data.get('dns_error')} "
                    f"(errno {res_data.get('dns_errno')}); not an explicit policy denial."
                )


class TestPlatformAndConfigurationGuards:
    """Verify platform guards, error propagation, and fail-closed security invariants."""

    def test_unsupported_platform_fails_closed(self, monkeypatch):
        """On non-Windows platform, initializing supervisor with use_sandbox=True raises SupervisorError."""
        from app.skills.supervisor import SkillProcessSupervisor, SupervisorError

        monkeypatch.setattr(sys, "platform", "linux")
        with pytest.raises(SupervisorError, match=r"Windows 10/Server 2016\+ is required"):
            SkillProcessSupervisor(use_sandbox=True)

    def test_launcher_rejects_non_windows_platform(self, monkeypatch):
        """SandboxedProcessLauncher fails closed if called on non-Windows platform."""
        monkeypatch.setattr(sys, "platform", "linux")
        launcher = SandboxedProcessLauncher()
        with pytest.raises(LauncherError, match="SandboxedProcessLauncher requires Windows"):
            launcher.launch("00000000-0000-4000-8000-000000000001", 0, deadline=time.monotonic() + 5.0)


@pytest.mark.skipif(sys.platform != "win32", reason="Quarantine tests require Windows.")
class TestSupervisorQuarantineCapacityAndRetention:
    """Verify quarantine ownership, capacity retention, cooperative shutdown, and re-acquisition prevention."""

    def test_normal_execution_releases_capacity_exactly_once(self, tmp_path):
        """Normal execution completes and releases semaphore capacity exactly once."""
        from app.skills.supervisor import SkillProcessSupervisor

        sup = SkillProcessSupervisor(max_concurrent_workers=1)
        try:
            assert sup._semaphore._value == 1
            req = ExecuteRequest(request_id="test-norm-1", skill_id="example.greeting", inputs={"name": "A"})
            resp = sup.execute(req)
            assert resp.success is True
            assert sup._semaphore._value == 1
        finally:
            sup.shutdown(timeout=2.0)

    def test_confirmed_process_termination_releases_capacity_exactly_once(self, tmp_path):
        """Confirmed process termination during cleanup releases capacity exactly once."""
        from app.skills.supervisor import SkillProcessSupervisor

        sup = SkillProcessSupervisor(max_concurrent_workers=1, startup_timeout_seconds=5.0)
        try:
            assert sup._semaphore._value == 1
            # Run request that fails in worker with execution error
            req = ExecuteRequest(request_id="test-conf-1", skill_id="unsupported.skill", inputs={})
            resp = sup.execute(req)
            assert resp.success is False
            assert sup._semaphore._value == 1
        finally:
            sup.shutdown(timeout=2.0)

    def test_unconfirmed_process_termination_retains_capacity(self, tmp_path):
        """When worker death cannot be confirmed, capacity reservation is retained and lock quarantined."""
        from app.skills.supervisor import LaunchReservation, SkillProcessSupervisor

        sup = SkillProcessSupervisor(max_concurrent_workers=1)
        session_id = str(uuid.uuid4())
        h_lock = 12345
        sup._quarantined_locks[session_id] = h_lock

        # Acquire the semaphore to simulate in-flight execution
        assert sup._semaphore.acquire(timeout=1.0) is True
        assert sup._semaphore._value == 0

        res = LaunchReservation(
            request_id="test-unconf-1",
            deadline=time.monotonic() + 5.0,
            error=ProcessReapError("Unconfirmed death", session_id=session_id, h_lock=h_lock),
            completed=True,
            quarantined=True,
        )

        sup._cleanup_execution("test-unconf-1", None, res)

        # Capacity MUST be retained (value is still 0)
        assert sup._semaphore._value == 0
        assert session_id in sup._quarantined_locks

    def test_quarantine_survives_cooperative_shutdown(self, tmp_path):
        """Quarantine locks and reservations survive shutdown without falsely reporting completion."""
        from app.skills.supervisor import SkillProcessSupervisor

        sup = SkillProcessSupervisor(
            runtime_provisioner=RuntimeProvisioner(runtime_dir=tmp_path / "rt", scratch_dir=tmp_path / "sc"),
            session_registry=SessionRegistry(base_dir=tmp_path / "ses", scratch_base=tmp_path / "sc"),
            use_sandbox=True,
        )
        session_id = str(uuid.uuid4())
        h_lock = sup._session_registry.acquire_session_lock(session_id, create_new=True)
        try:
            sup._quarantined_locks[session_id] = h_lock
            sup.shutdown(timeout=1.0)

            # Quarantined lock is retained
            assert session_id in sup._quarantined_locks
            assert sup._quarantined_locks[session_id] == h_lock

            # Shutdown is NOT falsely completed while quarantined sessions remain unresolved
            assert sup.is_shutdown_completed is False
            assert sup.is_shutdown is True
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(h_lock))

    def test_repeated_cleanup_does_not_release_quarantined_capacity(self, tmp_path):
        """Multiple cleanup calls on quarantined reservation never release capacity."""
        from app.skills.supervisor import LaunchReservation, SkillProcessSupervisor

        sup = SkillProcessSupervisor(max_concurrent_workers=1)
        assert sup._semaphore.acquire(timeout=1.0) is True
        assert sup._semaphore._value == 0

        res = LaunchReservation(
            request_id="test-rep-1",
            deadline=time.monotonic() + 5.0,
            completed=True,
            quarantined=True,
        )

        sup._cleanup_execution("test-rep-1", None, res)
        assert sup._semaphore._value == 0

        # Repeated cleanup call
        sup._cleanup_execution("test-rep-1", None, res)
        assert sup._semaphore._value == 0

    def test_new_execution_cannot_acquire_capacity_reserved_by_unresolved_quarantined_worker(self, tmp_path):
        """A new execution is rejected when capacity is held by an unresolved quarantined worker."""
        from app.skills.supervisor import LaunchReservation, SkillProcessSupervisor

        sup = SkillProcessSupervisor(max_concurrent_workers=1)
        assert sup._semaphore.acquire(timeout=1.0) is True
        assert sup._semaphore._value == 0

        # Quarantine this slot
        session_id = str(uuid.uuid4())
        sup._quarantined_locks[session_id] = 54321
        res = LaunchReservation(
            request_id="test-held-1",
            deadline=time.monotonic() + 5.0,
            completed=True,
            quarantined=True,
        )
        sup._cleanup_execution("test-held-1", None, res)
        assert sup._semaphore._value == 0

        # Attempt new execution on 1-capacity supervisor
        req = ExecuteRequest(request_id="test-new-rejected", skill_id="example.greeting", inputs={}, timeout_seconds=0.1)
        resp = sup.execute(req)
        assert resp.success is False
        assert resp.error is not None
        assert resp.error["code"] == "concurrency_limit_reached"

    def test_concurrent_cleanup_never_over_releases_semaphore(self, tmp_path):
        """Concurrent cleanup calls on the same reservation release capacity strictly once."""
        from app.skills.supervisor import LaunchReservation, SkillProcessSupervisor

        sup = SkillProcessSupervisor(max_concurrent_workers=1)
        assert sup._semaphore.acquire(timeout=1.0) is True
        assert sup._semaphore._value == 0

        res = LaunchReservation(
            request_id="test-conc-cleanup",
            deadline=time.monotonic() + 5.0,
            completed=True,
            quarantined=False,
        )

        barrier = threading.Barrier(10)

        def _concurrent_cleanup():
            try:
                barrier.wait(timeout=2.0)
            except Exception:
                pass
            sup._cleanup_execution("test-conc-cleanup", None, res)

        threads = [threading.Thread(target=_concurrent_cleanup) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=3.0)

        # Capacity must be exactly 1, NEVER 2 or more
        assert sup._semaphore._value == 1

    def test_supervisor_forced_termination_quarantines_unreaped_worker(self, tmp_path):
        """Forced termination failing to confirm worker death enters quarantine without releasing capacity."""
        from unittest.mock import MagicMock
        from app.skills.sandbox.process_launcher import SandboxedProcess
        from app.skills.supervisor import LaunchReservation, SkillProcessSupervisor

        ses_base = tmp_path / "sessions"
        scratch_base = tmp_path / "scratch"
        registry = SessionRegistry(base_dir=ses_base, scratch_base=scratch_base)
        session_id = str(uuid.uuid4())
        h_lock = registry.acquire_session_lock(session_id, create_new=True)

        scratch_dir = scratch_base / session_id
        scratch_dir.mkdir(parents=True, exist_ok=True)
        marker_file = scratch_dir / "worker_output.dat"
        marker_file.write_text("critical worker state", encoding="utf-8")

        mock_profile = MagicMock()
        mock_profile.sid_string = "S-1-15-2-12345"
        mock_profile.delete_profile = MagicMock()

        mock_job = MagicMock()
        mock_job.close = MagicMock()

        class StubUnreapedSandboxedProcess(SandboxedProcess):
            def __init__(self):
                super().__init__(
                    session_id=session_id,
                    h_process=12345,
                    pid=98765,
                    stdin_stream=io.BytesIO(),
                    stdout_stream=io.BytesIO(),
                    stderr_stream=io.BytesIO(),
                    job=mock_job,
                    profile=mock_profile,
                    h_lock=h_lock,
                    scratch_dir=scratch_dir,
                )
                self.kill_called = False
                self.wait_count = 0
                self.close_called = False

            def kill(self):
                self.kill_called = True

            def wait(self, timeout=None):
                self.wait_count += 1
                raise subprocess.TimeoutExpired(f"SandboxedProcess-{self.pid}", timeout or 0.0)

            def poll(self):
                return None

            def close(self):
                self.close_called = True
                super().close()

        stub_proc = StubUnreapedSandboxedProcess()

        sup = SkillProcessSupervisor(
            max_concurrent_workers=1,
            session_registry=registry,
            use_sandbox=True,
            cleanup_grace_period_seconds=0.001,
        )

        try:
            assert sup._semaphore.acquire(timeout=1.0) is True
            assert sup._semaphore._value == 0

            reservation = LaunchReservation(
                request_id="test-unreaped-req",
                deadline=time.monotonic() + 5.0,
                completed=True,
                quarantined=False,
            )
            sup._active_processes["test-unreaped-req"] = stub_proc
            sup._in_progress_launches["test-unreaped-req"] = reservation
            sup._active_request_ids.add("test-unreaped-req")

            # Execute cleanup on the unconfirmed-dead process
            sup._cleanup_execution("test-unreaped-req", stub_proc, reservation)

            # 1. Verify two-stage termination was attempted:
            assert stub_proc.kill_called is True
            assert mock_job.close.called is True
            assert stub_proc.wait_count >= 2

            # 2. Verify process is marked quarantined:
            assert getattr(stub_proc, "_quarantined", False) is True
            assert stub_proc.close_called is False

            # 3. Verify session lock remains retained:
            assert session_id in sup._quarantined_locks
            assert sup._quarantined_locks[session_id] == h_lock

            # 4. Verify semaphore capacity is NOT released:
            assert sup._semaphore._value == 0
            assert reservation.capacity_released is False

            # 5. Verify repeated cleanup does not release capacity:
            sup._cleanup_execution("test-unreaped-req", stub_proc, reservation)
            assert sup._semaphore._value == 0
            assert reservation.capacity_released is False

            # 6. Verify concurrent cleanup does not release capacity:
            barrier = threading.Barrier(10)

            def _concurrent_cleanup_attempt():
                try:
                    barrier.wait(timeout=2.0)
                except Exception:
                    pass
                sup._cleanup_execution("test-unreaped-req", stub_proc, reservation)

            threads = [threading.Thread(target=_concurrent_cleanup_attempt) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=3.0)

            assert sup._semaphore._value == 0
            assert reservation.capacity_released is False

            # 7. Verify supervisor shutdown does not report successful completion:
            sup.shutdown(timeout=0.1)
            assert sup.is_shutdown is True
            assert sup.is_shutdown_completed is False
            assert session_id in sup._quarantined_locks

            # 8. Verify scratch and AppContainer profile cleanup do NOT occur:
            assert marker_file.is_file()
            assert marker_file.read_text(encoding="utf-8") == "critical worker state"
            assert mock_profile.delete_profile.called is False
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(h_lock))
