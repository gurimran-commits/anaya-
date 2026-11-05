"""Implementations of notification handlers for logs, callbacks, and speech."""

from __future__ import annotations

import logging
from typing import Any, Callable

from app.notifications.models import ReminderNotification

logger = logging.getLogger(__name__)


class LoggingNotificationHandler:
    """Default minimal notification handler recording reminders to application logs."""

    def __init__(self, logger_instance: logging.Logger | None = None) -> None:
        self._logger = logger_instance or logger

    def notify(self, notification: ReminderNotification) -> None:
        """Log reminder notification."""
        self._logger.info(
            "Task Reminder [%s] (missed=%s): %s",
            notification.task_id,
            notification.is_missed,
            notification.message,
        )


class CallbackNotificationHandler:
    """Delegate reminder notifications to a custom user or UI callback function."""

    def __init__(self, callback: Callable[[ReminderNotification], None]) -> None:
        if not callable(callback):
            raise TypeError("Notification callback must be callable.")
        self._callback = callback

    def notify(self, notification: ReminderNotification) -> None:
        """Invoke custom callback."""
        self._callback(notification)


class CompositeNotificationHandler:
    """Fan out notifications to multiple registered handlers."""

    def __init__(self, handlers: list[Any] | None = None) -> None:
        self._handlers = list(handlers or [])

    def add_handler(self, handler: Any) -> None:
        """Register a notification handler."""
        self._handlers.append(handler)

    def notify(self, notification: ReminderNotification) -> None:
        """Deliver notification to all registered handlers sequentially."""
        for handler in self._handlers:
            try:
                if hasattr(handler, "notify") and callable(handler.notify):
                    handler.notify(notification)
                elif callable(handler):
                    handler(notification)
            except Exception as exc:
                logger.error("Error in notification handler %r: %s", handler, exc)


class SpeechNotificationHandler:
    """Optional speech output adapter speaking notifications via existing TTS interfaces.

    Does NOT modify the protected speech pipeline and does NOT activate any microphone.
    """

    def __init__(self, tts_engine: Any = None) -> None:
        self._tts_engine = tts_engine

    def notify(self, notification: ReminderNotification) -> None:
        """Speak reminder message if TTS engine is provided and capable."""
        if self._tts_engine is None:
            return

        try:
            # Check for synthesize_to_file or speak or similar existing TTS methods
            if hasattr(self._tts_engine, "speak") and callable(self._tts_engine.speak):
                self._tts_engine.speak(notification.message)
            elif hasattr(self._tts_engine, "synthesize") and callable(self._tts_engine.synthesize):
                self._tts_engine.synthesize(notification.message)
        except Exception as exc:
            logger.warning("Speech notification delivery failed: %s", exc)
