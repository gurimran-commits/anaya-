"""Runtime provisioner for isolated worker Python staging and scratch filesystem management.

Provides:
- Dedicated, immutable Python 3.12 runtime staging (python.exe, core DLLs, python312.zip,
  anaya_worker.zip, and isolated python312._pth without arbitrary path imports).
- Restrictive filesystem ACL enforcement for the staged runtime (Read & Execute for ALL
  APPLICATION PACKAGES, protected DACL).
- Per-session scratch directory provisioning with strict two-ACE DACL isolation:
  Root ACE: traverse, list, create files/subdirectories, no deletion of the root itself.
  Child Inherit ACE: full control (read/write/delete) exclusively for descendants.
- Zero-TOCTOU handle-relative recursive cleanup that safely unlinks junctions and symbolic
  links without traversing external target trees.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
import os
from pathlib import Path
import shutil
import sys
import zipfile

from app.skills.sandbox.appcontainer import validate_session_id
from app.skills.sandbox.win32_acl import (
    AclError,
    FilesystemCleanupError,
    build_explicit_access,
    clean_directory_handle_relative,
    convert_sid_to_string,
    convert_string_to_sid,
    create_protected_directory,
    delete_file_by_handle,
    grant_path_access,
)
from app.skills.sandbox.win32_acl_types import (
    CHILD_INHERIT_ONLY_ACE,
    DELETE,
    ERROR_FILE_NOT_FOUND,
    ERROR_SUCCESS,
    FILE_ALL_ACCESS,
    FILE_FLAG_BACKUP_SEMANTICS,
    FILE_FLAG_OPEN_REPARSE_POINT,
    FILE_SHARE_DELETE,
    FILE_SHARE_READ,
    FILE_SHARE_WRITE,
    GENERIC_READ,
    NO_INHERITANCE,
    OPEN_EXISTING,
    RUNTIME_ACCESS_MASK,
    SCRATCH_CHILD_ACCESS_MASK,
    SCRATCH_ROOT_ACCESS_MASK,
    SUB_CONTAINERS_AND_OBJECTS_INHERIT,
    SYNCHRONIZE,
    TokenUser,
    TRAVERSE_ACCESS_MASK,
    advapi32,
    is_invalid_handle,
    kernel32,
)

logger = logging.getLogger("anaya.skills.sandbox.runtime_provisioner")

# Well-known SDDL SIDs
SID_ALL_APPLICATION_PACKAGES: str = "S-1-15-2-1"
SID_ADMINISTRATORS: str = "S-1-5-32-544"
SID_LOCAL_SYSTEM: str = "S-1-5-18"

# Excluded directories when packaging stdlib python312.zip
STDLIB_EXCLUDE_DIRS: frozenset[str] = frozenset(
    {
        "test",
        "tests",
        "idlelib",
        "tkinter",
        "tcl8",
        "ensurepip",
        "site-packages",
        "distutils",
        "__pycache__",
    }
)

# Files packaged into anaya_worker.zip
WORKER_ALLOWLIST_FILES: tuple[str, ...] = (
    "app/skills/manifest.py",
    "app/skills/models.py",
    "app/skills/protocol.py",
    "app/skills/skill_registry.py",
    "app/skills/worker.py",
    "app/skills/builtin/__init__.py",
    "app/skills/builtin/example_skill.py",
)

# Worker-safe __init__.py content inside anaya_worker.zip
WORKER_INIT_CONTENT: str = """\"\"\"Worker-safe skills package initialization for sandboxed runtime.\"\"\"
from app.skills.builtin.example_skill import GreetingSkill, register_builtin_skills
from app.skills.models import (
    InputFieldSpec,
    Skill,
    SkillMetadata,
    SkillPermission,
    SkillResult,
    SkillStatus,
)
from app.skills.protocol import (
    PROTOCOL_VERSION,
    ExecuteRequest,
    ExecuteResponse,
    MessageType,
    ProtocolError,
    ProtocolSizeError,
    ProtocolValidationError,
    ProtocolVersionError,
    decode_request,
    decode_response,
    encode_request,
    encode_response,
)
from app.skills.skill_registry import SkillRegistry
"""


class RuntimeProvisionerError(Exception):
    """Base exception for all runtime provisioner errors."""


def get_current_user_sid_string() -> str:
    """Retrieve the SDDL string SID of the current process token."""
    if sys.platform != "win32" or advapi32 is None or kernel32 is None:
        return "S-1-5-32-544"

    GetCurrentProcess = kernel32.GetCurrentProcess
    GetCurrentProcess.restype = wintypes.HANDLE
    h_token = wintypes.HANDLE()

    if not advapi32.OpenProcessToken(GetCurrentProcess(), 0x0008, ctypes.byref(h_token)):  # TOKEN_QUERY
        err = ctypes.get_last_error()
        raise RuntimeProvisionerError(f"OpenProcessToken failed with Win32 error {err}.")

    try:
        size = wintypes.DWORD()
        advapi32.GetTokenInformation(h_token, TokenUser, None, 0, ctypes.byref(size))
        buf = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(h_token, TokenUser, buf, size.value, ctypes.byref(size)):
            err = ctypes.get_last_error()
            raise RuntimeProvisionerError(f"GetTokenInformation(TokenUser) failed with Win32 error {err}.")

        p_sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p)).contents.value
        return convert_sid_to_string(p_sid)
    finally:
        kernel32.CloseHandle(h_token)


class RuntimeProvisioner:
    """Manages staging of the isolated worker Python runtime and per-session scratch spaces."""

    def __init__(
        self,
        runtime_dir: Path | str | None = None,
        scratch_dir: Path | str | None = None,
    ) -> None:
        base_anaya = Path.home() / ".anaya" / "sandbox"
        self._runtime_dir = Path(runtime_dir).resolve() if runtime_dir is not None else (base_anaya / "runtime").resolve()
        self._scratch_dir = Path(scratch_dir).resolve() if scratch_dir is not None else (base_anaya / "scratch").resolve()

    @property
    def runtime_dir(self) -> Path:
        """The absolute filesystem path of the staged worker runtime directory."""
        return self._runtime_dir

    @property
    def scratch_dir(self) -> Path:
        """The base filesystem path for all worker session scratch directories."""
        return self._scratch_dir

    @property
    def worker_executable(self) -> Path:
        """The absolute path to the staged isolated python.exe."""
        return self._runtime_dir / "python.exe"

    def is_runtime_valid(self) -> bool:
        """Verify whether all required artifacts of the staged runtime exist and are valid."""
        if not self._runtime_dir.is_dir():
            return False

        if not (self._runtime_dir / "python.exe").is_file():
            return False

        dll_name = f"python{sys.version_info.major}{sys.version_info.minor}.dll"
        if not (self._runtime_dir / dll_name).is_file():
            return False

        py_zip = f"python{sys.version_info.major}{sys.version_info.minor}.zip"
        if not (self._runtime_dir / py_zip).is_file():
            return False

        if not (self._runtime_dir / "anaya_worker.zip").is_file():
            return False

        pth_file = self._runtime_dir / f"python{sys.version_info.major}{sys.version_info.minor}._pth"
        if not pth_file.is_file():
            return False

        try:
            content = pth_file.read_text(encoding="utf-8").strip().splitlines()
            expected = [py_zip, "anaya_worker.zip"]
            if content != expected:
                return False
        except Exception:
            return False

        return True

    def ensure_runtime_staged(self, force_rebuild: bool = False) -> Path:
        """Stage the immutable worker runtime if not already present or if rebuild requested.

        Artifacts:
        - python.exe, python3.dll, python312.dll, vcruntime140.dll, vcruntime140_1.dll
        - python312.zip (stdlib excluding test suites, idlelib, tkinter, site-packages, pycache)
        - anaya_worker.zip (allowlisted worker modules + safe __init__.py)
        - python312._pth (strict path configuration: python312.zip and anaya_worker.zip only)
        - DACL: Read & Execute for ALL APPLICATION PACKAGES, protected from parent inheritance.
        """
        if self.is_runtime_valid() and not force_rebuild:
            return self._runtime_dir

        self._runtime_dir.mkdir(parents=True, exist_ok=True)
        host_prefix = Path(sys.base_prefix).resolve()
        py_ver_suffix = f"{sys.version_info.major}{sys.version_info.minor}"
        py_zip_name = f"python{py_ver_suffix}.zip"
        pth_name = f"python{py_ver_suffix}._pth"

        # 1. Copy required binaries
        binaries = [
            "python.exe",
            "python3.dll",
            f"python{py_ver_suffix}.dll",
            "vcruntime140.dll",
            "vcruntime140_1.dll",
        ]
        for binary in binaries:
            src = host_prefix / binary
            dst = self._runtime_dir / binary
            if src.is_file():
                try:
                    shutil.copy2(src, dst)
                except Exception as err:
                    raise RuntimeProvisionerError(f"Failed to copy '{src}' to '{dst}': {err}") from err
            elif binary == "python.exe" or binary == f"python{py_ver_suffix}.dll":
                raise RuntimeProvisionerError(f"Required Python host binary missing: '{src}'.")

        # 2. Build stdlib zip (python312.zip)
        lib_dir = host_prefix / "Lib"
        if not lib_dir.is_dir():
            raise RuntimeProvisionerError(f"Python standard library directory not found: '{lib_dir}'.")

        target_py_zip = self._runtime_dir / py_zip_name
        tmp_py_zip = self._runtime_dir / f"{py_zip_name}.tmp"
        try:
            with zipfile.ZipFile(tmp_py_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for root, dirs, files in os.walk(lib_dir):
                    rel_root = Path(root).relative_to(lib_dir)
                    dirs[:] = [
                        d for d in dirs
                        if d not in STDLIB_EXCLUDE_DIRS and not d.startswith(".")
                    ]
                    if any(part in STDLIB_EXCLUDE_DIRS for part in rel_root.parts):
                        continue
                    for file_name in files:
                        if file_name.endswith((".pyc", ".pyo")):
                            continue
                        src_file = Path(root) / file_name
                        arcname = (rel_root / file_name).as_posix()
                        zf.write(src_file, arcname)

            if target_py_zip.exists():
                target_py_zip.unlink()
            tmp_py_zip.rename(target_py_zip)
        except Exception as err:
            tmp_py_zip.unlink(missing_ok=True)
            raise RuntimeProvisionerError(f"Failed to assemble stdlib archive '{target_py_zip}': {err}") from err

        # 3. Build worker code zip (anaya_worker.zip)
        target_worker_zip = self._runtime_dir / "anaya_worker.zip"
        tmp_worker_zip = self._runtime_dir / "anaya_worker.zip.tmp"
        repo_root = Path(__file__).resolve().parent.parent.parent.parent

        try:
            with zipfile.ZipFile(tmp_worker_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("app/__init__.py", "")
                zf.writestr("app/skills/__init__.py", WORKER_INIT_CONTENT)

                for rel_path in WORKER_ALLOWLIST_FILES:
                    src_file = repo_root / rel_path
                    if not src_file.is_file():
                        raise RuntimeProvisionerError(f"Required worker codebase file missing: '{src_file}'.")
                    zf.write(src_file, rel_path)

            if target_worker_zip.exists():
                target_worker_zip.unlink()
            tmp_worker_zip.rename(target_worker_zip)
        except Exception as err:
            tmp_worker_zip.unlink(missing_ok=True)
            raise RuntimeProvisionerError(f"Failed to assemble worker codebase archive: {err}") from err

        # 4. Apply filesystem permissions on Windows BEFORE completing path activation
        if sys.platform == "win32" and advapi32 is not None and kernel32 is not None:
            self._apply_runtime_dacl(self._runtime_dir)

        # 5. Write isolated ._pth file to finalize runtime staging
        target_pth = self._runtime_dir / pth_name
        pth_content = f"{py_zip_name}\nanaya_worker.zip\n"
        try:
            target_pth.write_text(pth_content, encoding="utf-8")
        except Exception as err:
            raise RuntimeProvisionerError(f"Failed to write '{target_pth}': {err}") from err

        logger.info("Worker runtime staged successfully at %s", self._runtime_dir)
        return self._runtime_dir

    def _apply_runtime_dacl(self, path: Path) -> None:
        """Grant Read & Execute to ALL APPLICATION PACKAGES, full control to Administrators/User."""
        current_user_sid = get_current_user_sid_string()
        sids_to_free: list[int] = []

        try:
            p_app_pkg = convert_string_to_sid(SID_ALL_APPLICATION_PACKAGES)
            sids_to_free.append(p_app_pkg)

            p_user = convert_string_to_sid(current_user_sid)
            sids_to_free.append(p_user)

            p_admin = convert_string_to_sid(SID_ADMINISTRATORS)
            sids_to_free.append(p_admin)

            p_system = convert_string_to_sid(SID_LOCAL_SYSTEM)
            sids_to_free.append(p_system)

            ea_app_pkg = build_explicit_access(
                p_app_pkg,
                RUNTIME_ACCESS_MASK,
                SUB_CONTAINERS_AND_OBJECTS_INHERIT,
            )
            ea_user = build_explicit_access(
                p_user,
                FILE_ALL_ACCESS,
                SUB_CONTAINERS_AND_OBJECTS_INHERIT,
            )
            ea_admin = build_explicit_access(
                p_admin,
                FILE_ALL_ACCESS,
                SUB_CONTAINERS_AND_OBJECTS_INHERIT,
            )
            ea_system = build_explicit_access(
                p_system,
                FILE_ALL_ACCESS,
                SUB_CONTAINERS_AND_OBJECTS_INHERIT,
            )

            grant_path_access(
                path,
                [ea_app_pkg, ea_user, ea_admin, ea_system],
                protect_dacl=True,
                replace_dacl=True,
            )
        except Exception as err:
            raise RuntimeProvisionerError(f"Failed to apply security descriptor to staged runtime '{path}': {err}") from err
        finally:
            for p_sid in sids_to_free:
                kernel32.LocalFree(ctypes.c_void_p(p_sid))

    def create_scratch_dir(self, session_id: str, worker_sid: int | str) -> Path:
        """Create and ACL-isolate a per-session scratch directory.

        Invariants:
        1. Base scratch parent has traverse access without directory listing.
        2. Session directory has SE_DACL_PROTECTED applied.
        3. Worker SID receives:
           - Root ACE: SCRATCH_ROOT_ACCESS_MASK (0x001201BF), NO_INHERITANCE (0x00).
           - Child Inherit ACE: SCRATCH_CHILD_ACCESS_MASK (0x0013019F), CHILD_INHERIT_ONLY_ACE (0x0B).
        4. ALL APPLICATION PACKAGES is explicitly excluded from the session scratch directory.
        """
        validated_id = validate_session_id(session_id)
        scratch_path = Path(os.path.abspath(str(self._scratch_dir / validated_id)))

        if scratch_path.exists():
            raise FileExistsError(f"Scratch directory already exists: '{scratch_path}'.")

        # Ensure parent scratch base directory exists
        self._ensure_scratch_parent_configured()

        if sys.platform != "win32" or advapi32 is None or kernel32 is None:
            scratch_path.mkdir(parents=True, exist_ok=False)
            return scratch_path

        # Resolve worker binary SID
        allocated_worker_sid = False
        if isinstance(worker_sid, str):
            p_worker_sid = convert_string_to_sid(worker_sid)
            allocated_worker_sid = True
        else:
            p_worker_sid = int(worker_sid)

        current_user_sid = get_current_user_sid_string()
        sids_to_free: list[int] = []

        try:
            p_user = convert_string_to_sid(current_user_sid)
            sids_to_free.append(p_user)

            p_admin = convert_string_to_sid(SID_ADMINISTRATORS)
            sids_to_free.append(p_admin)

            p_system = convert_string_to_sid(SID_LOCAL_SYSTEM)
            sids_to_free.append(p_system)

            # Full control for supervisor, admin, system
            ea_user = build_explicit_access(p_user, FILE_ALL_ACCESS, SUB_CONTAINERS_AND_OBJECTS_INHERIT)
            ea_admin = build_explicit_access(p_admin, FILE_ALL_ACCESS, SUB_CONTAINERS_AND_OBJECTS_INHERIT)
            ea_system = build_explicit_access(p_system, FILE_ALL_ACCESS, SUB_CONTAINERS_AND_OBJECTS_INHERIT)

            # Two-ACE DACL strictly for worker SID
            ea_worker_root = build_explicit_access(
                p_worker_sid,
                SCRATCH_ROOT_ACCESS_MASK,
                NO_INHERITANCE,
            )
            ea_worker_child = build_explicit_access(
                p_worker_sid,
                SCRATCH_CHILD_ACCESS_MASK,
                CHILD_INHERIT_ONLY_ACE,
            )

            create_protected_directory(
                scratch_path,
                [ea_user, ea_admin, ea_system, ea_worker_root, ea_worker_child],
            )
        except Exception as err:
            raise RuntimeProvisionerError(f"Failed to create protected scratch directory '{scratch_path}': {err}") from err
        finally:
            if allocated_worker_sid:
                kernel32.LocalFree(ctypes.c_void_p(p_worker_sid))
            for p_sid in sids_to_free:
                kernel32.LocalFree(ctypes.c_void_p(p_sid))

        return scratch_path

    def _ensure_scratch_parent_configured(self) -> None:
        """Ensure parent scratch root directory exists with traverse-only access for AppContainers."""
        if self._scratch_dir.exists():
            return

        self._scratch_dir.mkdir(parents=True, exist_ok=True)

        if sys.platform != "win32" or advapi32 is None or kernel32 is None:
            return

        current_user_sid = get_current_user_sid_string()
        sids_to_free: list[int] = []
        try:
            p_app_pkg = convert_string_to_sid(SID_ALL_APPLICATION_PACKAGES)
            sids_to_free.append(p_app_pkg)

            p_user = convert_string_to_sid(current_user_sid)
            sids_to_free.append(p_user)

            p_admin = convert_string_to_sid(SID_ADMINISTRATORS)
            sids_to_free.append(p_admin)

            p_system = convert_string_to_sid(SID_LOCAL_SYSTEM)
            sids_to_free.append(p_system)

            ea_user = build_explicit_access(p_user, FILE_ALL_ACCESS, SUB_CONTAINERS_AND_OBJECTS_INHERIT)
            ea_admin = build_explicit_access(p_admin, FILE_ALL_ACCESS, SUB_CONTAINERS_AND_OBJECTS_INHERIT)
            ea_system = build_explicit_access(p_system, FILE_ALL_ACCESS, SUB_CONTAINERS_AND_OBJECTS_INHERIT)
            # Traverse only without listing, no inheritance
            ea_app_pkg = build_explicit_access(p_app_pkg, TRAVERSE_ACCESS_MASK, NO_INHERITANCE)

            grant_path_access(self._scratch_dir, [ea_user, ea_admin, ea_system, ea_app_pkg])
        except Exception as err:
            raise RuntimeProvisionerError(
                f"Failed to configure scratch parent permissions on '{self._scratch_dir}': {err}"
            ) from err
        finally:
            for p_sid in sids_to_free:
                kernel32.LocalFree(ctypes.c_void_p(p_sid))

    def cleanup_scratch_dir(self, scratch_path: Path | str) -> bool:
        """Safely clean up a scratch directory tree using handle-relative enumeration and unlinking.

        Guarantees that reparse points (junctions, symbolic links) are unlinked without
        recursing into or deleting external directory targets.
        Strictly zero path-based fallback.
        """
        target = Path(os.path.abspath(str(scratch_path)))
        if not target.exists():
            return True

        if sys.platform != "win32" or kernel32 is None:
            raise RuntimeProvisionerError("Scratch cleanup requires Windows kernel.")

        # Open directory handle with DELETE access and backup semantics
        h_dir = kernel32.CreateFileW(
            str(target),
            DELETE | GENERIC_READ | SYNCHRONIZE,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )

        if is_invalid_handle(h_dir):
            err = ctypes.get_last_error()
            if err == ERROR_FILE_NOT_FOUND:
                return True
            raise FilesystemCleanupError(
                f"Failed to open scratch directory handle for '{target}' with Win32 error {err}."
            )

        try:
            # 1. Clean all child entries handle-relatively
            clean_directory_handle_relative(h_dir)
            # 2. Unlink the root scratch directory handle
            delete_file_by_handle(h_dir)
        except Exception as err:
            logger.error("Handle-relative cleanup failed for '%s': %s", target, err)
            raise

        return not target.exists()
