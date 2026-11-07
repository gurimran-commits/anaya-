"""OS-level sandbox facilities for skill worker processes.

Provides kernel-enforced process containment and resource controls:
- win32_job: Windows Job Object management, memory caps, active process limits, and kill-on-close
- win32_types: Win32 API ctypes structures and kernel32 signatures
"""

from __future__ import annotations

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
]
