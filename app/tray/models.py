"""Data models, constants, and exceptions for Windows system tray handling."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

# Windows Message Codes
WM_NULL = 0x0000
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_QUIT = 0x0012
WM_USER = 0x0400
WM_COMMAND = 0x0111
WM_CONTEXTMENU = 0x007B
WM_RBUTTONUP = 0x0205
WM_LBUTTONUP = 0x0202

# Custom Application Window Messages
WM_TRAY_CALLBACK = WM_USER + 20
WM_TRAY_REFRESH = WM_USER + 21

# Tray Notification Events (under NOTIFYICON_VERSION_4)
NIN_SELECT = WM_USER
NIN_KEYSELECT = WM_USER + 1
NIN_BALLOONSHOW = WM_USER + 2
NIN_BALLOONHIDE = WM_USER + 3
NIN_BALLOONTIMEOUT = WM_USER + 4
NIN_BALLOONUSERCLICK = WM_USER + 5

# Shell NotifyIcon Action Codes
NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIM_SETVERSION = 0x00000004

# Shell NotifyIcon Flags
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIF_STATE = 0x00000008
NIF_INFO = 0x00000010
NIF_GUID = 0x00000020
NIF_SHOWTIP = 0x00000080

NOTIFYICON_VERSION_4 = 4

# Win32 Menu Flags
MF_STRING = 0x00000000
MF_GRAYED = 0x00000001
MF_DISABLED = 0x00000002
MF_CHECKED = 0x00000008
MF_UNCHECKED = 0x00000000
MF_POPUP = 0x00000010
MF_SEPARATOR = 0x00000800
MF_DEFAULT = 0x00001000

# TrackPopupMenu Flags
TPM_RIGHTBUTTON = 0x0002
TPM_BOTTOMALIGN = 0x0020

# Standard Menu Command Identifiers
IDM_OPEN = 1001
IDM_TOGGLE_HOTKEY = 1002
IDM_STATUS_HEADER = 1003
IDM_STATUS_ASSISTANT = 1004
IDM_STATUS_HOTKEY = 1005
IDM_STATUS_AI = 1006
IDM_STATUS_SPEECH = 1007
IDM_EXIT = 1008


class TrayServiceState(str, Enum):
    """Explicit lifecycle states for WindowsTrayService."""

    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    TIMEOUT_STOPPED = "TIMEOUT_STOPPED"


class TrayError(Exception):
    """Base exception for all system tray errors."""


class TrayRegistrationError(TrayError):
    """Raised when registering or modifying the system tray icon fails."""


class TrayVersionError(TrayError):
    """Raised when setting NOTIFYICON_VERSION_4 fails."""


@dataclass(frozen=True)
class TrayStatusSnapshot:
    """Immutable point-in-time snapshot of application subsystem states.

    Note:
        Constructed from sequential reads of thread-safe subsystem properties.
        Represents a best-effort point-in-time operational view without requiring
        a global cross-subsystem lock.
    """

    controller_running: bool
    hotkey_running: bool
    hotkey_state: str
    hotkey_combination: str
    voice_active: bool
    speech_available: bool
    ai_enabled: bool
    ai_profile: str


def sanitize_and_truncate_tooltip(text: str, max_code_units: int = 127) -> str:
    """Truncate text to fit within max_code_units UTF-16 code units without splitting surrogate pairs.

    The Windows NOTIFYICONDATAW.szTip buffer holds exactly 128 WCHARs (UTF-16 code units),
    leaving at most 127 code units plus the null terminator. Slicing by Python string length
    can overflow if non-BMP characters (surrogate pairs, e.g. emoji) are present, or split
    a surrogate pair leaving a dangling high surrogate.

    Args:
        text: Raw tooltip text.
        max_code_units: Maximum permitted UTF-16 code units (defaults to 127).

    Returns:
        Safely truncated Unicode string.
    """
    clean_text = text.strip()
    encoded = clean_text.encode("utf-16-le")
    max_bytes = max_code_units * 2

    if len(encoded) <= max_bytes:
        return clean_text

    truncated = encoded[:max_bytes]

    # Inspect the trailing 16-bit word: if it is a high surrogate (0xD800 to 0xDBFF),
    # drop it so we never leave an unpaired dangling surrogate.
    last_word = int.from_bytes(truncated[-2:], "little")
    if 0xD800 <= last_word <= 0xDBFF:
        truncated = truncated[:-2]

    return truncated.decode("utf-16-le")


def capture_status_snapshot(
    controller: Any,
    hotkey_service: Any = None,
) -> TrayStatusSnapshot:
    """Capture an immutable point-in-time snapshot using verified thread-safe properties.

    Safely reads fast in-memory properties without invoking network checks (e.g. ModelManager.is_available()).
    """
    hk_running = False
    hk_state = "UNAVAILABLE"
    hk_combo = "None"
    voice_active = False

    if hotkey_service is not None:
        try:
            hk_running = bool(hotkey_service.is_running)
            hk_state = str(hotkey_service.state.value if hasattr(hotkey_service.state, "value") else hotkey_service.state)
            if hasattr(hotkey_service, "combination") and hasattr(hotkey_service.combination, "canonical_string"):
                hk_combo = str(hotkey_service.combination.canonical_string)
            voice_active = bool(hotkey_service.is_voice_active)
        except Exception:
            pass

    ctrl_running = False
    speech_avail = False
    ai_enabled = False
    ai_profile = "none"

    if controller is not None:
        try:
            ctrl_running = bool(controller.is_running)
            speech_avail = bool(controller.voice_pipeline is not None)
            mm = getattr(controller, "model_manager", None)
            if mm is not None:
                ai_enabled = bool(getattr(mm, "is_enabled", False))
                profile = getattr(mm, "active_profile", None)
                if profile is not None:
                    ai_profile = str(getattr(profile, "value", profile))
        except Exception:
            pass

    return TrayStatusSnapshot(
        controller_running=ctrl_running,
        hotkey_running=hk_running,
        hotkey_state=hk_state,
        hotkey_combination=hk_combo,
        voice_active=voice_active,
        speech_available=speech_avail,
        ai_enabled=ai_enabled,
        ai_profile=ai_profile,
    )
