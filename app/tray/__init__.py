"""Anaya Windows System Tray subsystem."""

from app.tray.backend import TrayBackend, Win32TrayBackend
from app.tray.models import (
    TrayError,
    TrayRegistrationError,
    TrayServiceState,
    TrayStatusSnapshot,
    TrayVersionError,
    capture_status_snapshot,
    sanitize_and_truncate_tooltip,
)
from app.tray.service import WindowsTrayService

__all__ = [
    "WindowsTrayService",
    "TrayBackend",
    "Win32TrayBackend",
    "TrayServiceState",
    "TrayStatusSnapshot",
    "TrayError",
    "TrayRegistrationError",
    "TrayVersionError",
    "capture_status_snapshot",
    "sanitize_and_truncate_tooltip",
]
