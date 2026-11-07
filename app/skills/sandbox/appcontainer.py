"""AppContainer profile management and lifecycle control for isolated workers.

Provides deterministic, per-worker AppContainer profile creation, verified SID ownership,
string formatting, and clean native memory deallocation under Python ctypes.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import re
import sys
from typing import Any

from app.skills.sandbox.win32_acl import convert_sid_to_string
from app.skills.sandbox.win32_acl_types import (
    HRESULT_ALREADY_EXISTS,
    HRESULT_FILE_NOT_FOUND,
    HRESULT_S_OK,
    advapi32,
    userenv,
)

# Strict session identifier pattern (canonical UUIDv4 or 32-character hex)
SESSION_ID_PATTERN: re.Pattern[str] = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$|^[0-9a-f]{32}$",
    re.IGNORECASE,
)


class AppContainerError(Exception):
    """Base exception for all Windows AppContainer errors."""


class InvalidSessionIdError(AppContainerError):
    """Raised when a session identifier violates strict formatting constraints."""


class ProfileCreationError(AppContainerError):
    """Raised when an AppContainer profile cannot be created or derived."""


class ProfileDeletionError(AppContainerError):
    """Raised when an AppContainer profile cannot be removed."""


def validate_session_id(session_id: str) -> str:
    """Validate that a session identifier is a well-formed UUIDv4 or 32-character hex string."""
    if not isinstance(session_id, str) or not session_id.strip():
        raise InvalidSessionIdError("Session ID must be a non-empty string.")

    cleaned = session_id.strip().lower()
    if not SESSION_ID_PATTERN.fullmatch(cleaned):
        raise InvalidSessionIdError(
            f"Session ID '{session_id}' is invalid. Must be a UUIDv4 or 32-char hex string."
        )
    return cleaned


class AppContainerProfile:
    """Manages the creation, native SID ownership, and teardown of a dedicated AppContainer profile."""

    def __init__(self, session_id: str) -> None:
        if sys.platform != "win32" or userenv is None or advapi32 is None:
            raise AppContainerError("AppContainerProfile requires Windows.")

        self._session_id: str = validate_session_id(session_id)
        self._profile_name: str = f"Anaya.Worker.{self._session_id}"
        self._sid_ptr: int | None = None
        self._sid_string: str | None = None
        self._is_closed: bool = False

    @property
    def session_id(self) -> str:
        """The validated unique session identifier."""
        return self._session_id

    @property
    def profile_name(self) -> str:
        """The deterministic AppContainer profile name."""
        return self._profile_name

    @property
    def sid(self) -> int:
        """The native binary PSID address. Raises if not initialized or closed."""
        if self._is_closed or self._sid_ptr is None:
            raise AppContainerError("Profile is closed or SID is not initialized.")
        return self._sid_ptr

    @property
    def sid_string(self) -> str:
        """The formatted SDDL SID string (e.g. S-1-15-2-...)."""
        if self._sid_string is None:
            raise AppContainerError("Profile SID has not been initialized.")
        return self._sid_string

    def ensure_created(self) -> None:
        """Create the AppContainer profile or derive its SID if it already exists.

        Idempotent:
        - If CreateAppContainerProfile returns HRESULT_ALREADY_EXISTS (0x800700B7),
          calls DeriveAppContainerSidFromAppContainerName to acquire the identical binary PSID.
        """
        if self._is_closed:
            raise AppContainerError("Cannot create a closed profile.")
        if self._sid_ptr is not None:
            return  # Already created and holding valid SID

        p_sid = ctypes.c_void_p()
        hr = userenv.CreateAppContainerProfile(
            self._profile_name,
            f"Anaya Sandbox Worker {self._session_id[:8]}",
            "Anaya Isolated External Skill Worker Execution Container",
            None,  # No capabilities granted
            0,
            ctypes.byref(p_sid),
        )

        if hr == HRESULT_S_OK:
            self._sid_ptr = int(p_sid.value or 0)
        elif hr == HRESULT_ALREADY_EXISTS or (hr & 0xFFFFFFFF) == 0x800700B7:
            # Derive identical binary PSID without failing
            p_derived = ctypes.c_void_p()
            hr_derive = userenv.DeriveAppContainerSidFromAppContainerName(
                self._profile_name,
                ctypes.byref(p_derived),
            )
            if hr_derive != HRESULT_S_OK:
                raise ProfileCreationError(
                    f"DeriveAppContainerSidFromAppContainerName failed for '{self._profile_name}' "
                    f"with HRESULT {hex(hr_derive & 0xFFFFFFFF)}."
                )
            self._sid_ptr = int(p_derived.value or 0)
        else:
            raise ProfileCreationError(
                f"CreateAppContainerProfile failed for '{self._profile_name}' "
                f"with HRESULT {hex(hr & 0xFFFFFFFF)}."
            )

        # Derive and cache the SDDL string representation
        self._sid_string = convert_sid_to_string(self._sid_ptr)

    def delete_profile(self) -> None:
        """Delete the AppContainer profile mapping from the operating system.

        Idempotent:
        - Treats HRESULT_FILE_NOT_FOUND (0x80070002) as success.
        - Does NOT free self._sid_ptr; close() must be called to release native memory.
        """
        if sys.platform != "win32" or userenv is None:
            raise AppContainerError("AppContainerProfile requires Windows.")

        hr = userenv.DeleteAppContainerProfile(self._profile_name)
        if hr != HRESULT_S_OK and hr != HRESULT_FILE_NOT_FOUND and (hr & 0xFFFFFFFF) != 0x80070002:
            raise ProfileDeletionError(
                f"DeleteAppContainerProfile failed for '{self._profile_name}' "
                f"with HRESULT {hex(hr & 0xFFFFFFFF)}."
            )

    def close(self) -> None:
        """Release the native binary PSID allocation via advapi32.FreeSid.

        Safe and idempotent.
        """
        if self._is_closed:
            return

        if self._sid_ptr is not None:
            advapi32.FreeSid(ctypes.c_void_p(self._sid_ptr))
            self._sid_ptr = None

        self._is_closed = True

    def __enter__(self) -> AppContainerProfile:
        self.ensure_created()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
