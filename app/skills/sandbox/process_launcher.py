"""Extended process launcher for isolated AppContainer and Job Object workers.

Enforces kernel-level security guarantees during process creation:
- Atomic Job Object association at creation (PROC_THREAD_ATTRIBUTE_JOB_LIST).
- AppContainer token identity with zero capabilities (PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES).
- Strict stdio pipe inheritance whitelist (PROC_THREAD_ATTRIBUTE_HANDLE_LIST).
- Pre-resume verification (Job membership, TokenIsAppContainer, TokenAppContainerSid, TokenCapabilities).
- Durable session metadata persistence (STATUS_WORKER_SUSPENDED) before ResumeThread.
- Strict two-stage process-death confirmation and quarantine ownership.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import io
import logging
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any

from app.skills.sandbox.appcontainer import (
    AppContainerError,
    AppContainerProfile,
    validate_session_id,
)
from app.skills.sandbox.runtime_provisioner import (
    RuntimeProvisioner,
    RuntimeProvisionerError,
)
from app.skills.sandbox.session_registry import (
    STATUS_SCRATCH_READY,
    STATUS_WORKER_RUNNING,
    STATUS_WORKER_SUSPENDED,
    SessionRegistry,
    SessionRegistryError,
    get_process_creation_time,
    open_verified_session_scratch_handle,
)
from app.skills.sandbox.win32_acl import (
    clean_directory_handle_relative,
    convert_sid_to_string,
    delete_file_by_handle,
)
from app.skills.sandbox.win32_job import (
    JobLimits,
    JobObjectError,
    Win32JobObject,
)
from app.skills.sandbox.win32_process_types import (
    CREATE_SUSPENDED,
    CREATE_UNICODE_ENVIRONMENT,
    ERROR_INSUFFICIENT_BUFFER,
    EXTENDED_STARTUPINFO_PRESENT,
    HANDLE_FLAG_INHERIT,
    PROCESS_INFORMATION,
    PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
    PROC_THREAD_ATTRIBUTE_JOB_LIST,
    PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
    SECURITY_CAPABILITIES,
    SID_AND_ATTRIBUTES,
    STARTF_USESTDHANDLES,
    STARTUPINFOEXW,
    STILL_ACTIVE,
    TOKEN_APPCONTAINER_INFORMATION,
    TOKEN_GROUPS,
    TOKEN_QUERY,
    TokenAppContainerSid,
    TokenCapabilities,
    TokenIsAppContainer,
    WAIT_OBJECT_0,
    WAIT_TIMEOUT,
    advapi32,
    kernel32,
)

logger = logging.getLogger("anaya.skills.sandbox.launcher")


class LauncherError(Exception):
    """Base exception for all sandboxed process launcher errors."""


class ProcessStartupError(LauncherError):
    """Raised when CreateProcessW fails to create the child process."""


class PreResumeVerificationError(LauncherError):
    """Raised when pre-resume security assertions (token, SID, Job) fail."""


class ProcessReapError(LauncherError):
    """Raised when worker process death cannot be confirmed by the OS kernel."""

    def __init__(self, message: str, session_id: str, h_lock: int | None = None) -> None:
        super().__init__(message)
        self.session_id = session_id
        self.h_lock = h_lock


def create_sanitized_environment(
    extra_env: dict[str, str] | None = None,
    runtime_dir: Path | str | None = None,
) -> ctypes.Array:
    """Create a double-null-terminated wide-character Unicode environment block.

    Preserves Windows native environment layout (including per-drive current directories),
    strips host PYTHONPATH, PYTHONHOME, and sensitive credential tokens, and points PATH
    to the isolated runtime directory.
    """
    if sys.platform != "win32" or kernel32 is None:
        return ctypes.create_unicode_buffer("\0\0")

    GetEnvironmentStringsW = kernel32.GetEnvironmentStringsW
    GetEnvironmentStringsW.restype = wintypes.LPVOID
    FreeEnvironmentStringsW = kernel32.FreeEnvironmentStringsW
    FreeEnvironmentStringsW.argtypes = [wintypes.LPVOID]
    FreeEnvironmentStringsW.restype = wintypes.BOOL

    ptr = GetEnvironmentStringsW()
    if not ptr:
        return ctypes.create_unicode_buffer("\0\0")

    entries: list[str] = []
    try:
        curr = ptr
        while True:
            s = ctypes.wstring_at(curr)
            if not s:
                break
            entries.append(s)
            curr += (len(s) + 1) * 2
    finally:
        FreeEnvironmentStringsW(ptr)

    filtered: list[str] = []
    disallowed_prefixes = (
        "PYTHONPATH=",
        "PYTHONHOME=",
        "ANTIGRAVITY_CSRF_TOKEN=",
        "TOKEN=",
        "SECRET=",
        "API_KEY=",
    )

    for entry in entries:
        # Preserve hidden Windows drive entries like "=C:=C:\..."
        if entry.startswith("="):
            filtered.append(entry)
            continue

        upper = entry.upper()
        if any(upper.startswith(p) for p in disallowed_prefixes):
            continue

        if runtime_dir is not None and upper.startswith("PATH="):
            filtered.append(f"PATH={runtime_dir}")
            continue

        filtered.append(entry)

    if extra_env:
        for k, v in extra_env.items():
            if k.upper() not in ("PYTHONPATH", "PYTHONHOME"):
                filtered.append(f"{k}={v}")

    block_str = "\0".join(filtered) + "\0\0"
    return ctypes.create_unicode_buffer(block_str)


def confirm_process_death_and_reap(
    h_process: int,
    h_thread: int,
    job: Win32JobObject,
    timeout_ms: int = 5000,
    fallback_timeout_ms: int = 2000,
) -> bool:
    """Synchronously verify process termination with fallback to Job Object kill-on-close.

    Returns True ONLY if the process object entered signaled state.
    Returns False if process death could not be confirmed.
    """
    if sys.platform != "win32" or kernel32 is None:
        return True

    # Stage 1: Direct TerminateProcess
    term_ok = bool(kernel32.TerminateProcess(wintypes.HANDLE(h_process), 1))
    if term_ok:
        wait_res = kernel32.WaitForSingleObject(wintypes.HANDLE(h_process), timeout_ms)
        if wait_res == WAIT_OBJECT_0:
            return True
        logger.warning(
            "TerminateProcess wait timed out (res=%#010x). Escalating to Job Object kill-on-close.",
            wait_res,
        )
    else:
        logger.warning(
            "TerminateProcess failed with Win32 error %d. Escalating to Job Object kill-on-close.",
            ctypes.get_last_error(),
        )

    # Stage 2: Fallback Job Object kill-on-close
    try:
        job.close()
    except Exception as job_exc:
        logger.critical("Failed to close Job Object during abort escalation: %s", job_exc)

    wait_res2 = kernel32.WaitForSingleObject(wintypes.HANDLE(h_process), fallback_timeout_ms)
    return wait_res2 == WAIT_OBJECT_0


def verify_appcontainer_process_token(
    h_process: int,
    expected_sid_string: str,
    expected_sid_ptr: int | None = None,
) -> None:
    """Verify that the process token is an AppContainer matching the exact session SID and zero capabilities."""
    if sys.platform != "win32" or advapi32 is None or kernel32 is None:
        raise PreResumeVerificationError("Token verification requires Windows.")

    h_token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(wintypes.HANDLE(h_process), TOKEN_QUERY, ctypes.byref(h_token)):
        err = ctypes.get_last_error()
        raise PreResumeVerificationError(f"OpenProcessToken(TOKEN_QUERY) failed with Win32 error {err}.")

    try:
        # 1. Query TokenIsAppContainer
        is_appcontainer = wintypes.DWORD(0)
        ret_len = wintypes.DWORD(0)
        if not advapi32.GetTokenInformation(
            h_token,
            TokenIsAppContainer,
            ctypes.byref(is_appcontainer),
            ctypes.sizeof(is_appcontainer),
            ctypes.byref(ret_len),
        ):
            err = ctypes.get_last_error()
            raise PreResumeVerificationError(f"GetTokenInformation(TokenIsAppContainer) failed with error {err}.")

        if is_appcontainer.value == 0:
            raise PreResumeVerificationError("Security violation: Process token is NOT an AppContainer token!")

        # 2. Query TokenAppContainerSid
        needed = wintypes.DWORD(0)
        advapi32.GetTokenInformation(h_token, TokenAppContainerSid, None, 0, ctypes.byref(needed))
        err = ctypes.get_last_error()
        if err != ERROR_INSUFFICIENT_BUFFER or needed.value == 0:
            raise PreResumeVerificationError(f"Querying TokenAppContainerSid buffer size failed with error {err}.")

        sid_buf = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(h_token, TokenAppContainerSid, sid_buf, needed.value, ctypes.byref(needed)):
            err = ctypes.get_last_error()
            raise PreResumeVerificationError(f"GetTokenInformation(TokenAppContainerSid) failed with error {err}.")

        app_info = ctypes.cast(sid_buf, ctypes.POINTER(TOKEN_APPCONTAINER_INFORMATION)).contents
        p_token_sid = app_info.TokenAppContainer
        if not p_token_sid:
            raise PreResumeVerificationError("TokenAppContainerSid returned a NULL SID pointer.")

        actual_sid_string = convert_sid_to_string(p_token_sid)
        if actual_sid_string != expected_sid_string:
            raise PreResumeVerificationError(
                f"AppContainer SID mismatch! Expected '{expected_sid_string}', got '{actual_sid_string}'."
            )

        if expected_sid_ptr is not None:
            if not advapi32.EqualSid(wintypes.LPVOID(p_token_sid), wintypes.LPVOID(expected_sid_ptr)):
                raise PreResumeVerificationError("Binary EqualSid comparison failed between token SID and profile SID.")

        # 3. Query TokenCapabilities (must have zero capabilities)
        needed_cap = wintypes.DWORD(0)
        advapi32.GetTokenInformation(h_token, TokenCapabilities, None, 0, ctypes.byref(needed_cap))
        err = ctypes.get_last_error()
        if err != ERROR_INSUFFICIENT_BUFFER or needed_cap.value == 0:
            raise PreResumeVerificationError(f"Querying TokenCapabilities buffer size failed with error {err}.")

        cap_buf = ctypes.create_string_buffer(needed_cap.value)
        if not advapi32.GetTokenInformation(h_token, TokenCapabilities, cap_buf, needed_cap.value, ctypes.byref(needed_cap)):
            err = ctypes.get_last_error()
            raise PreResumeVerificationError(f"GetTokenInformation(TokenCapabilities) failed with error {err}.")

        groups = ctypes.cast(cap_buf, ctypes.POINTER(TOKEN_GROUPS)).contents
        if groups.GroupCount != 0:
            raise PreResumeVerificationError(
                f"Security violation: Process token was granted {groups.GroupCount} unintended capabilities! "
                "Sandboxed workers must have zero capabilities."
            )

    finally:
        kernel32.CloseHandle(h_token)


class SandboxedProcess:
    """Wraps a native Win32 process running under AppContainer and Job Object controls.

    Exposes an interface fully compatible with subprocess.Popen for supervisor consumption:
    .stdin, .stdout, .stderr, .poll(), .wait(timeout), .kill(), .returncode, .close().
    """

    def __init__(
        self,
        session_id: str,
        h_process: int,
        pid: int,
        stdin_stream: io.BufferedWriter,
        stdout_stream: io.BufferedReader,
        stderr_stream: io.BufferedReader,
        job: Win32JobObject,
        profile: AppContainerProfile,
        h_lock: int,
        scratch_dir: Path,
    ) -> None:
        self.session_id = session_id
        self._h_process: int | None = h_process
        self.pid = pid
        self.stdin = stdin_stream
        self.stdout = stdout_stream
        self.stderr = stderr_stream
        self.job = job
        self.profile = profile
        self.h_lock = h_lock
        self.scratch_dir = scratch_dir
        self.returncode: int | None = None
        self._closed = False

    def poll(self) -> int | None:
        """Check if process has terminated without blocking."""
        if self.returncode is not None:
            return self.returncode
        if self._h_process is None or sys.platform != "win32" or kernel32 is None:
            return None

        code = wintypes.DWORD()
        if kernel32.GetExitCodeProcess(wintypes.HANDLE(self._h_process), ctypes.byref(code)):
            if code.value != STILL_ACTIVE:
                self.returncode = int(code.value)
                return self.returncode
        return None

    def wait(self, timeout: float | None = None) -> int:
        """Wait for the process to terminate within the given timeout seconds."""
        if self.returncode is not None:
            return self.returncode
        if self._h_process is None or sys.platform != "win32" or kernel32 is None:
            return 0

        timeout_ms = int(timeout * 1000) if timeout is not None else 0xFFFFFFFF
        wait_res = kernel32.WaitForSingleObject(wintypes.HANDLE(self._h_process), timeout_ms)
        if wait_res == WAIT_TIMEOUT:
            raise subprocess.TimeoutExpired(f"SandboxedProcess-{self.pid}", timeout or 0.0)

        code = wintypes.DWORD()
        if kernel32.GetExitCodeProcess(wintypes.HANDLE(self._h_process), ctypes.byref(code)):
            self.returncode = int(code.value)
        else:
            self.returncode = 1
        return self.returncode

    def kill(self) -> None:
        """Forcefully terminate the process."""
        if self._h_process is not None and sys.platform == "win32" and kernel32 is None:
            return
        if self._h_process is not None:
            try:
                kernel32.TerminateProcess(wintypes.HANDLE(self._h_process), 1)
            except Exception:
                pass

    def close_pipes(self) -> None:
        """Close stdio streams safely."""
        for stream in (self.stdin, self.stdout, self.stderr):
            if stream is not None and not stream.closed:
                try:
                    stream.close()
                except Exception:
                    pass

    def close(self) -> None:
        """Release all native resources associated with this process."""
        if self._closed:
            return
        self._closed = True
        self.close_pipes()
        if self._h_process is not None and sys.platform == "win32" and kernel32 is not None:
            try:
                kernel32.CloseHandle(wintypes.HANDLE(self._h_process))
            except Exception:
                pass
            self._h_process = None
        try:
            self.job.close()
        except Exception:
            pass


class SandboxedProcessLauncher:
    """Manages the creation, verification, and atomic launch of sandboxed worker processes."""

    def __init__(
        self,
        runtime_provisioner: RuntimeProvisioner | None = None,
        session_registry: SessionRegistry | None = None,
    ) -> None:
        self._provisioner = runtime_provisioner or RuntimeProvisioner()
        self._registry = session_registry or SessionRegistry()

    @property
    def provisioner(self) -> RuntimeProvisioner:
        """The underlying runtime provisioner."""
        return self._provisioner

    @property
    def registry(self) -> SessionRegistry:
        """The underlying session registry."""
        return self._registry

    def launch(
        self,
        session_id: str,
        h_lock: int,
        deadline: float,
        memory_limit_bytes: int | None = 256 * 1024 * 1024,
        package_to_stage: Path | str | None = None,
        skills_root: Path | str | None = None,
    ) -> SandboxedProcess:
        """Execute the atomic 12-step process launch under AppContainer and Job Object containment."""
        if sys.platform != "win32" or kernel32 is None or advapi32 is None:
            raise LauncherError("SandboxedProcessLauncher requires Windows 10 Version 1703+ or Server 2016+.")

        import msvcrt

        validated_id = validate_session_id(session_id)

        # 1. Ensure runtime is staged and valid
        self._provisioner.ensure_runtime_staged()
        worker_exe = self._provisioner.worker_executable
        if not worker_exe.is_file():
            raise ProcessStartupError(f"Staged worker executable not found at '{worker_exe}'.")

        # 2. Create AppContainer profile
        profile = AppContainerProfile(validated_id)
        profile.ensure_created()

        # 3. Provision per-session scratch directory with 2-ACE DACL
        scratch_dir = self._provisioner.create_scratch_dir(validated_id, profile.sid_string)

        # 4. Durable metadata write: STATUS_SCRATCH_READY
        self._registry.write_durable_metadata(
            validated_id,
            {
                "session_id": validated_id,
                "profile_name": profile.profile_name,
                "supervisor_pid": os.getpid(),
                "supervisor_start_time": get_process_creation_time(os.getpid()),
                "worker_pid": None,
                "worker_start_time": None,
                "status": STATUS_SCRATCH_READY,
                "scratch_path": str(scratch_dir),
                "created_at_utc": datetime.now(timezone.utc).isoformat(),
            },
        )

        # 4.5. Stage external skill package if provided
        if package_to_stage is not None:
            from app.skills.staging import stage_external_package, StagingError
            try:
                stage_external_package(
                    package_source_path=package_to_stage,
                    session_scratch_dir=scratch_dir,
                    worker_sid=profile.sid_string,
                    skills_root=skills_root,
                )
            except StagingError as stg_err:
                raise ProcessStartupError(f"Failed to stage external skill package: {stg_err}") from stg_err
            except Exception as exc:
                raise ProcessStartupError(f"Unexpected error staging external skill package: {exc}") from exc

        # 5. Create Job Object with approved limits
        limits = JobLimits(
            kill_on_close=True,
            active_process_limit=1,
            process_memory_limit_bytes=memory_limit_bytes,
        )
        job = Win32JobObject(limits=limits)

        # 6. Create anonymous pipes for stdin, stdout, stderr
        h_child_in = wintypes.HANDLE()
        h_parent_in = wintypes.HANDLE()
        if not kernel32.CreatePipe(ctypes.byref(h_child_in), ctypes.byref(h_parent_in), None, 0):
            job.close()
            raise ProcessStartupError("Failed to create stdin pipe.")

        h_parent_out = wintypes.HANDLE()
        h_child_out = wintypes.HANDLE()
        if not kernel32.CreatePipe(ctypes.byref(h_parent_out), ctypes.byref(h_child_out), None, 0):
            kernel32.CloseHandle(h_child_in)
            kernel32.CloseHandle(h_parent_in)
            job.close()
            raise ProcessStartupError("Failed to create stdout pipe.")

        h_parent_err = wintypes.HANDLE()
        h_child_err = wintypes.HANDLE()
        if not kernel32.CreatePipe(ctypes.byref(h_parent_err), ctypes.byref(h_child_err), None, 0):
            kernel32.CloseHandle(h_child_in)
            kernel32.CloseHandle(h_parent_in)
            kernel32.CloseHandle(h_parent_out)
            kernel32.CloseHandle(h_child_out)
            job.close()
            raise ProcessStartupError("Failed to create stderr pipe.")

        # 7. Configure handle inheritance: child ends inheritable, parent ends private
        kernel32.SetHandleInformation(h_child_in, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT)
        kernel32.SetHandleInformation(h_child_out, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT)
        kernel32.SetHandleInformation(h_child_err, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT)

        kernel32.SetHandleInformation(h_parent_in, HANDLE_FLAG_INHERIT, 0)
        kernel32.SetHandleInformation(h_parent_out, HANDLE_FLAG_INHERIT, 0)
        kernel32.SetHandleInformation(h_parent_err, HANDLE_FLAG_INHERIT, 0)

        # Attribute list buffer allocation and initialization
        attr_size = ctypes.c_size_t(0)
        kernel32.InitializeProcThreadAttributeList(None, 3, 0, ctypes.byref(attr_size))
        attr_buf = ctypes.create_string_buffer(attr_size.value)
        if not kernel32.InitializeProcThreadAttributeList(attr_buf, 3, 0, ctypes.byref(attr_size)):
            err = ctypes.get_last_error()
            self._close_all_raw_pipe_handles(
                h_child_in, h_parent_in, h_parent_out, h_child_out, h_parent_err, h_child_err
            )
            job.close()
            raise ProcessStartupError(f"InitializeProcThreadAttributeList failed with Win32 error {err}.")

        h_process_int: int | None = None
        h_thread_int: int | None = None
        dw_pid: int = 0
        creation_time: int | None = None

        try:
            # 8. Set Attribute 1: Job List (atomic assignment)
            h_job_arr = (wintypes.HANDLE * 1)(job.handle)
            if not kernel32.UpdateProcThreadAttribute(
                attr_buf,
                0,
                PROC_THREAD_ATTRIBUTE_JOB_LIST,
                h_job_arr,
                ctypes.sizeof(h_job_arr),
                None,
                None,
            ):
                err = ctypes.get_last_error()
                raise ProcessStartupError(f"UpdateProcThreadAttribute(JOB_LIST) failed with error {err}.")

            # 9. Set Attribute 2: Handle List (strict 3-pipe inheritance)
            inherited_handles = (wintypes.HANDLE * 3)(h_child_in, h_child_out, h_child_err)
            if not kernel32.UpdateProcThreadAttribute(
                attr_buf,
                0,
                PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                inherited_handles,
                ctypes.sizeof(inherited_handles),
                None,
                None,
            ):
                err = ctypes.get_last_error()
                raise ProcessStartupError(f"UpdateProcThreadAttribute(HANDLE_LIST) failed with error {err}.")

            # 10. Set Attribute 3: Security Capabilities (AppContainer identity, zero capabilities)
            sec_cap = SECURITY_CAPABILITIES()
            sec_cap.AppContainerSid = wintypes.LPVOID(profile.sid)
            sec_cap.Capabilities = None
            sec_cap.CapabilityCount = 0
            sec_cap.Reserved = 0
            if not kernel32.UpdateProcThreadAttribute(
                attr_buf,
                0,
                PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
                ctypes.byref(sec_cap),
                ctypes.sizeof(sec_cap),
                None,
                None,
            ):
                err = ctypes.get_last_error()
                raise ProcessStartupError(f"UpdateProcThreadAttribute(SECURITY_CAPABILITIES) failed with error {err}.")

            # 11. Build STARTUPINFOEXW
            si = STARTUPINFOEXW()
            si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
            si.StartupInfo.dwFlags = STARTF_USESTDHANDLES
            si.StartupInfo.hStdInput = h_child_in
            si.StartupInfo.hStdOutput = h_child_out
            si.StartupInfo.hStdError = h_child_err
            si.lpAttributeList = ctypes.cast(attr_buf, wintypes.LPVOID)

            pi = PROCESS_INFORMATION()

            # Prepare command line and environment
            cmd_str = f'"{worker_exe}" -m app.skills.worker'
            cmd_buf = ctypes.create_unicode_buffer(cmd_str)
            env_buf = create_sanitized_environment(runtime_dir=self._provisioner.runtime_dir)

            creation_flags = (
                CREATE_SUSPENDED
                | EXTENDED_STARTUPINFO_PRESENT
                | CREATE_UNICODE_ENVIRONMENT
            )

            # 12. CreateProcessW
            res = kernel32.CreateProcessW(
                str(worker_exe),
                cmd_buf,
                None,
                None,
                True,  # bInheritHandles must be True for PROC_THREAD_ATTRIBUTE_HANDLE_LIST
                creation_flags,
                ctypes.cast(env_buf, wintypes.LPVOID),
                str(scratch_dir),
                ctypes.byref(si),
                ctypes.byref(pi),
            )
            if not res:
                err = ctypes.get_last_error()
                raise ProcessStartupError(f"CreateProcessW failed with Win32 error {err}.")

            h_process_int = int(pi.hProcess)
            h_thread_int = int(pi.hThread)
            dw_pid = int(pi.dwProcessId)

            # Close child pipe handles in parent immediately after process creation
            kernel32.CloseHandle(h_child_in)
            kernel32.CloseHandle(h_child_out)
            kernel32.CloseHandle(h_child_err)
            h_child_in = None
            h_child_out = None
            h_child_err = None

            # 13. Pre-Resume Verification 1: Verify Job Membership
            is_in_job = wintypes.BOOL(False)
            if not kernel32.IsProcessInJob(pi.hProcess, wintypes.HANDLE(job.handle), ctypes.byref(is_in_job)):
                err = ctypes.get_last_error()
                raise PreResumeVerificationError(f"IsProcessInJob query failed with Win32 error {err}.")
            if not is_in_job.value:
                raise PreResumeVerificationError("Security violation: Process is NOT in the target Job Object!")

            # 14. Pre-Resume Verification 2: Verify Token & AppContainer SID
            verify_appcontainer_process_token(
                h_process_int,
                expected_sid_string=profile.sid_string,
                expected_sid_ptr=profile.sid,
            )

            # 15. Record worker suspended with PID and FILETIME before resume
            creation_time = get_process_creation_time(dw_pid)
            if creation_time is None:
                raise PreResumeVerificationError(f"Failed to query creation FILETIME for worker PID {dw_pid}.")

            self._registry.record_worker_suspended(
                validated_id,
                worker_pid=dw_pid,
                worker_start_time=creation_time,
            )

            # 16. Resume the worker thread
            res_thread = kernel32.ResumeThread(pi.hThread)
            if res_thread == 0xFFFFFFFF:
                err = ctypes.get_last_error()
                raise PreResumeVerificationError(f"ResumeThread failed with Win32 error {err}.")

            # Close thread handle immediately as it is no longer needed
            kernel32.CloseHandle(pi.hThread)
            h_thread_int = None

            # 17. Update state to worker running
            self._registry.record_worker_resumed(validated_id)

            # 18. Convert parent pipe handles to Python buffered streams via msvcrt
            fd_in = msvcrt.open_osfhandle(int(h_parent_in.value), os.O_WRONLY)
            h_parent_in = None  # fd_in now owns the OS handle
            stdin_stream = io.open(fd_in, "wb", buffering=0)

            fd_out = msvcrt.open_osfhandle(int(h_parent_out.value), os.O_RDONLY)
            h_parent_out = None  # fd_out now owns the OS handle
            stdout_stream = io.open(fd_out, "rb", buffering=io.DEFAULT_BUFFER_SIZE)

            fd_err = msvcrt.open_osfhandle(int(h_parent_err.value), os.O_RDONLY)
            h_parent_err = None  # fd_err now owns the OS handle
            stderr_stream = io.open(fd_err, "rb", buffering=io.DEFAULT_BUFFER_SIZE)

            # Successfully initialized sandboxed process
            sandboxed_proc = SandboxedProcess(
                session_id=validated_id,
                h_process=h_process_int,
                pid=dw_pid,
                stdin_stream=stdin_stream,
                stdout_stream=stdout_stream,
                stderr_stream=stderr_stream,
                job=job,
                profile=profile,
                h_lock=h_lock,
                scratch_dir=scratch_dir,
            )
            # Ownership transferred to SandboxedProcess
            h_process_int = None
            return sandboxed_proc

        except Exception as exc:
            logger.error("Sandboxed process launch failed for session '%s': %s", validated_id, exc)

            # Close child pipe handles if still open
            if h_child_in is not None:
                kernel32.CloseHandle(h_child_in)
            if h_child_out is not None:
                kernel32.CloseHandle(h_child_out)
            if h_child_err is not None:
                kernel32.CloseHandle(h_child_err)

            # Close parent pipe handles if still unconverted
            if h_parent_in is not None:
                kernel32.CloseHandle(h_parent_in)
            if h_parent_out is not None:
                kernel32.CloseHandle(h_parent_out)
            if h_parent_err is not None:
                kernel32.CloseHandle(h_parent_err)

            # If process was created, execute strict synchronous reap
            if h_process_int is not None:
                death_confirmed = confirm_process_death_and_reap(
                    h_process=h_process_int,
                    h_thread=h_thread_int or 0,
                    job=job,
                )
                if not death_confirmed:
                    # Quarantine ownership: Process death could NOT be confirmed!
                    # Do not delete scratch or profile. Retain session lock handle.
                    raise ProcessReapError(
                        f"Worker PID {dw_pid} could not be confirmed dead. Session '{validated_id}' quarantined with lock held.",
                        session_id=validated_id,
                        h_lock=h_lock,
                    ) from exc

                # Process confirmed dead: clean up thread and process handles
                if h_thread_int is not None:
                    kernel32.CloseHandle(wintypes.HANDLE(h_thread_int))
                kernel32.CloseHandle(wintypes.HANDLE(h_process_int))

            # Process confirmed dead or never created: perform safe cleanup
            try:
                job.close()
            except Exception:
                pass

            try:
                self._provisioner.cleanup_scratch_dir(scratch_dir)
            except Exception as scratch_exc:
                logger.warning("Failed to clean scratch directory after abort for session '%s': %s", validated_id, scratch_exc)

            try:
                profile.close()
            except Exception as prof_exc:
                logger.warning("Failed to delete AppContainer profile after abort for session '%s': %s", validated_id, prof_exc)

            raise

        finally:
            kernel32.DeleteProcThreadAttributeList(attr_buf)

    @staticmethod
    def _close_all_raw_pipe_handles(*handles: wintypes.HANDLE) -> None:
        """Close any non-null raw pipe handles."""
        if sys.platform != "win32" or kernel32 is None:
            return
        for h in handles:
            if h is not None and int(h) != 0:
                try:
                    kernel32.CloseHandle(h)
                except Exception:
                    pass
