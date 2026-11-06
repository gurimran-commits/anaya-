"""Anaya Global Hotkey subsystem."""

from app.hotkey.backend import HotkeyBackend, Win32HotkeyBackend
from app.hotkey.models import (
    MOD_ALT,
    MOD_CONTROL,
    MOD_NOREPEAT,
    MOD_SHIFT,
    MOD_WIN,
    HotkeyCombination,
    HotkeyConfigurationError,
    HotkeyConflictError,
    HotkeyError,
    HotkeyRegistrationError,
    HotkeyServiceState,
)
from app.hotkey.service import GlobalHotkeyService

__all__ = [
    "GlobalHotkeyService",
    "HotkeyBackend",
    "Win32HotkeyBackend",
    "HotkeyCombination",
    "HotkeyServiceState",
    "HotkeyError",
    "HotkeyConfigurationError",
    "HotkeyRegistrationError",
    "HotkeyConflictError",
    "MOD_ALT",
    "MOD_CONTROL",
    "MOD_SHIFT",
    "MOD_WIN",
    "MOD_NOREPEAT",
]
