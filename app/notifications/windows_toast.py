"""Windows Action Center and Desktop Toast notification adapter."""

from __future__ import annotations

import logging
import sys
from typing import Any, Callable

from app.notifications.handlers import LoggingNotificationHandler
from app.notifications.models import ReminderNotification
from app.notifications.protocols import NotificationHandler

logger = logging.getLogger(__name__)

# Default Application User Model ID (AUMID) for Anaya
DEFAULT_AUMID = "Anaya.DesktopAssistant"

# Verified target package specification
VERIFIED_WINDOWS_TOASTS_VERSION = "windows-toasts>=1.3.1,<2.0.0"


class WindowsToastNotificationHandler:
    """Delivers reminder notifications via native Windows Action Center toasts.

    Platform & Dependency Requirements:
        - Target OS: Windows 10 (Build 19041+) or Windows 11.
        - Verified Package: windows-toasts>=1.3.1,<2.0.0 (optional dependency).
        - Underlying runtime: winrt-runtime and winrt-Windows.UI.Notifications.

    Action Center Persistence vs. Transient Banners:
        - Setting the process AUMID alone allows displaying transient on-screen toast banners.
        - Genuine persistence in Windows Action Center history requires an explicit Start Menu
          shortcut (.lnk) in %APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs containing the
          System.AppUserModel.ID property matching the configured AUMID.
        - This handler does NOT automatically create or modify Start Menu shortcuts or registry entries.

    Silent OS Notification Suppression:
        - When Windows Focus Assist (Windows 10) or Do Not Disturb (Windows 11) is active,
          or if notifications for this application are disabled in Windows Settings (System > Notifications),
          the Windows Shell will silently suppress or discard toast banners. This is normal OS behavior.

    Graceful Degradation:
        - If executed on non-Windows platforms (sys.platform != 'win32') or if windows-toasts is not installed,
          the handler automatically routes notifications to the fallback LoggingNotificationHandler
          without raising exceptions or halting execution.
    """

    def __init__(
        self,
        aumid: str = DEFAULT_AUMID,
        fallback_handler: NotificationHandler | None = None,
        toaster_factory: Callable[[str], Any] | None = None,
        toast_factory: Callable[..., Any] | None = None,
        set_process_aumid: bool = True,
    ) -> None:
        self._aumid = aumid.strip() if aumid and aumid.strip() else DEFAULT_AUMID
        self._fallback_handler = fallback_handler or LoggingNotificationHandler()
        self._toaster_factory = toaster_factory
        self._toast_factory = toast_factory
        self._set_process_aumid = set_process_aumid
        self._process_aumid_set = False

        # Attempt to set process explicit AUMID once on Windows if requested
        if self._set_process_aumid and sys.platform == "win32":
            self._init_process_aumid()

    @property
    def aumid(self) -> str:
        """Configured Application User Model ID (AUMID)."""
        return self._aumid

    @property
    def fallback_handler(self) -> NotificationHandler:
        """Configured fallback notification handler."""
        return self._fallback_handler

    def _init_process_aumid(self) -> None:
        """Set process-level explicit AUMID via Win32 Shell API if available."""
        if self._process_aumid_set:
            return
        try:
            import ctypes
            if hasattr(ctypes, "windll") and hasattr(ctypes.windll, "shell32"):
                hr = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(self._aumid)
                if hr == 0:
                    self._process_aumid_set = True
                    logger.debug("Successfully set process explicit AUMID to '%s'.", self._aumid)
        except Exception as exc:
            logger.debug("Could not set process explicit AUMID (%s): %s", type(exc).__name__, exc)

    def is_available(self) -> bool:
        """Return True if Windows toast delivery is available on this system."""
        if self._toaster_factory is not None:
            return True

        if sys.platform != "win32":
            return False

        try:
            import windows_toasts  # noqa: F401
            return True
        except ImportError:
            return False

    def notify(self, notification: ReminderNotification) -> None:
        """Deliver a reminder notification via Windows Action Center toast.

        If windows-toasts is unavailable or the host is not Windows, safely
        degrades to fallback logging.
        """
        if not isinstance(notification, ReminderNotification):
            self._route_to_fallback(
                notification,
                reason="Invalid notification payload: expected ReminderNotification instance.",
            )
            return

        if sys.platform != "win32" and self._toaster_factory is None:
            self._route_to_fallback(
                notification,
                reason=f"WindowsToastNotificationHandler unavailable on platform '{sys.platform}'.",
            )
            return

        try:
            # Resolve toaster and toast classes
            if self._toaster_factory is not None:
                toaster = self._toaster_factory(self._aumid)
                toast = self._toast_factory() if self._toast_factory else None
            else:
                try:
                    from windows_toasts import Toast, WindowsToaster
                    toaster = WindowsToaster(self._aumid)
                    toast = Toast()
                except ImportError as imp_err:
                    self._route_to_fallback(
                        notification,
                        reason=(
                            f"windows-toasts package is not installed ({imp_err}). "
                            "Desktop toasts require optional dependency 'windows-toasts'."
                        ),
                    )
                    return

            # Construct toast payload
            title = notification.title or "Anaya Task Reminder"
            body = notification.message or f"Task '{title}' is due."

            # Set text fields: title and body
            if toast is not None and hasattr(toast, "text_fields"):
                toast.text_fields = [title, body]
            elif toast is None and self._toast_factory is not None:
                toast = self._toast_factory(title=title, body=body)

            # Display toast
            if hasattr(toaster, "show_toast") and callable(toaster.show_toast):
                toaster.show_toast(toast)
            else:
                raise TypeError(f"Toaster instance {toaster!r} lacks callable show_toast method.")

            logger.info(
                "Windows toast notification dispatched for task '%s' (title=%r).",
                notification.task_id,
                title,
            )

        except Exception as exc:
            logger.warning(
                "Windows toast notification delivery failed for task '%s' (%s: %s); routing to fallback.",
                notification.task_id,
                type(exc).__name__,
                exc,
            )
            self._route_to_fallback(
                notification,
                reason=f"Windows toast delivery error ({type(exc).__name__}: {exc}).",
            )

    def _route_to_fallback(self, notification: Any, reason: str) -> None:
        """Route notification to fallback handler safely."""
        logger.info("%s", reason)
        try:
            if isinstance(notification, ReminderNotification) and self._fallback_handler is not None:
                self._fallback_handler.notify(notification)
        except Exception as fallback_exc:
            try:
                logger.critical("Critical error in Windows toast fallback handler: %s", fallback_exc)
            except Exception:
                pass
