"""Protocols for runtime notification dispatchers."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.notifications.models import ReminderNotification


@runtime_checkable
class NotificationHandler(Protocol):
    """Protocol for delivering reminder notifications to UI, logging, or speech."""

    def notify(self, notification: ReminderNotification) -> None:
        """Deliver a reminder notification.

        Args:
            notification: The structured reminder payload to deliver.
        """
        ...
