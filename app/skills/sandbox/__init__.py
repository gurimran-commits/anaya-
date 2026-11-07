"""OS-level sandbox facilities for skill worker processes.

Provides kernel-enforced process containment, memory limits, and filesystem isolation:
- win32_job: Windows Job Object management, memory caps, active process limits, and kill-on-close.
- win32_types: Win32 Job Object ctypes structures and kernel32 signatures.
- win32_acl_types: Win32 Access Control and native NT ctypes definitions, constants, and function prototypes.
- win32_acl: Protected DACL creation, handle-relative zero-TOCTOU filesystem cleanup, and traversal checks.
- appcontainer: AppContainer profile lifecycle management and unique per-worker identity isolation.
- session_registry: Crash-safe session tracking, permanent tombstone locks, and write-ahead metadata.
- runtime_provisioner: Dedicated immutable Python runtime staging and per-session scratch ACL provisioning.
"""

from __future__ import annotations

from app.skills.sandbox.appcontainer import (
    AppContainerError,
    AppContainerProfile,
    InvalidSessionIdError,
    ProfileCreationError,
    ProfileDeletionError,
    validate_session_id,
)
from app.skills.sandbox.runtime_provisioner import (
    RuntimeProvisioner,
    RuntimeProvisionerError,
    get_current_user_sid_string,
)
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
    SessionRegistryError,
    check_process_liveness,
    get_process_creation_time,
    is_process_alive,
    validate_metadata_schema,
    verify_ordinary_directory_not_reparse,
    open_verified_session_scratch_handle,
)
from app.skills.sandbox.win32_acl import (
    AclError,
    FilesystemCleanupError,
    NativeApiError,
    SecurityDescriptorError,
    SecurityPolicyConflictError,
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
)
from app.skills.sandbox.win32_acl_types import (
    CHILD_INHERIT_ONLY_ACE,
    EXPLICIT_ACCESS_W,
    FILE_ALL_ACCESS,
    FILE_BASIC_INFO,
    FILE_GENERIC_EXECUTE,
    FILE_GENERIC_READ,
    FILE_GENERIC_WRITE,
    FileBasicInfo,
    NO_INHERITANCE,
    PROTECTED_DACL_SECURITY_INFORMATION,
    RUNTIME_ACCESS_MASK,
    SCRATCH_CHILD_ACCESS_MASK,
    SCRATCH_ROOT_ACCESS_MASK,
    SECURITY_ATTRIBUTES,
    SECURITY_DESCRIPTOR,
    SUB_CONTAINERS_AND_OBJECTS_INHERIT,
    TRAVERSE_ACCESS_MASK,
    TRUSTEE_W,
)
from app.skills.sandbox.win32_job import (
    JobAccountingInfo,
    JobAssignmentError,
    JobCloseError,
    JobLimits,
    JobObjectCreationError,
    JobObjectError,
    JobObjectLimitError,
    JobObjectQueryError,
    JobTerminationError,
    Win32JobObject,
)
from app.skills.sandbox.win32_types import (
    IO_COUNTERS,
    JOBOBJECT_BASIC_ACCOUNTING_INFORMATION,
    JOBOBJECT_BASIC_LIMIT_INFORMATION,
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
)

__all__ = [
    # Step 10.3.1 Job Objects
    "Win32JobObject",
    "JobLimits",
    "JobAccountingInfo",
    "JobObjectError",
    "JobObjectCreationError",
    "JobObjectLimitError",
    "JobAssignmentError",
    "JobObjectQueryError",
    "JobTerminationError",
    "JobCloseError",
    "IO_COUNTERS",
    "JOBOBJECT_BASIC_LIMIT_INFORMATION",
    "JOBOBJECT_EXTENDED_LIMIT_INFORMATION",
    "JOBOBJECT_BASIC_ACCOUNTING_INFORMATION",
    # Step 10.3.2 AppContainer Profiles
    "AppContainerProfile",
    "AppContainerError",
    "InvalidSessionIdError",
    "ProfileCreationError",
    "ProfileDeletionError",
    "validate_session_id",
    # Step 10.3.2 Session Registry & Locking
    "SessionRegistry",
    "SessionRegistryError",
    "SessionLockError",
    "SessionMetadataError",
    "SessionActiveError",
    "ProcessLiveness",
    "STATUS_PROFILE_PENDING",
    "STATUS_PROFILE_CREATED",
    "STATUS_SCRATCH_READY",
    "STATUS_WORKER_RUNNING",
    "STATUS_COMPLETED",
    "ALLOWED_METADATA_FIELDS",
    "MAX_METADATA_BYTES",
    "get_process_creation_time",
    "check_process_liveness",
    "is_process_alive",
    "validate_metadata_schema",
    "verify_ordinary_directory_not_reparse",
    "open_verified_session_scratch_handle",
    # Step 10.3.2 Runtime & Scratch Provisioner
    "RuntimeProvisioner",
    "RuntimeProvisionerError",
    "get_current_user_sid_string",
    # Step 10.3.2 Win32 ACL & Native Handle Operations
    "AclError",
    "SecurityDescriptorError",
    "SecurityPolicyConflictError",
    "NativeApiError",
    "FilesystemCleanupError",
    "convert_sid_to_string",
    "convert_string_to_sid",
    "build_explicit_access",
    "create_protected_directory",
    "grant_path_access",
    "check_traverse_preflight",
    "open_relative_child_handle",
    "delete_file_by_handle",
    "clean_directory_handle_relative",
    "self_test_native_nt_api",
    # Step 10.3.2 Win32 ACL Types & Constants
    "TRUSTEE_W",
    "EXPLICIT_ACCESS_W",
    "SECURITY_DESCRIPTOR",
    "SECURITY_ATTRIBUTES",
    "FILE_BASIC_INFO",
    "FileBasicInfo",
    "FILE_ALL_ACCESS",
    "FILE_GENERIC_READ",
    "FILE_GENERIC_WRITE",
    "FILE_GENERIC_EXECUTE",
    "RUNTIME_ACCESS_MASK",
    "PROTECTED_DACL_SECURITY_INFORMATION",
    "SCRATCH_ROOT_ACCESS_MASK",
    "SCRATCH_CHILD_ACCESS_MASK",
    "TRAVERSE_ACCESS_MASK",
    "NO_INHERITANCE",
    "SUB_CONTAINERS_AND_OBJECTS_INHERIT",
    "CHILD_INHERIT_ONLY_ACE",
]
