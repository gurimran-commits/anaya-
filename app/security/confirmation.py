from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable
from uuid import uuid4

@dataclass(frozen=True)
class PendingConfirmation:
    confirmation_id: str
    action: str
    arguments: tuple[str, ...]
    expires_at: datetime


class ConfirmationManager:
    """Create and validate short-lived action confirmations."""

    def __init__(
        self,
        expiry_seconds: int = 60,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if expiry_seconds <= 0:
            raise ValueError("expiry_seconds must be positive")

        self._expiry_seconds = expiry_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._pending: dict[str, PendingConfirmation] = {}

    def request(
        self,
        action: str,
        arguments: tuple[str, ...] = (),
    ) -> PendingConfirmation:
        if not action.strip():
            raise ValueError("Action must not be empty")

        confirmation = PendingConfirmation(
            confirmation_id=str(uuid4()),
            action=action,
            arguments=tuple(arguments),
           expires_at=self._clock() + timedelta(seconds=self._expiry_seconds),
        )

        self._pending[confirmation.confirmation_id] = confirmation
        return confirmation

    def approve(
        self,
        confirmation_id: str,
        action: str,
        arguments: tuple[str, ...] = (),
    ) -> bool:
        pending = self._pending.pop(confirmation_id, None)

        if pending is None:
            return False

        if self._clock() >= pending.expires_at:
            return False

        return (
            pending.action == action
            and pending.arguments == tuple(arguments)
        )

    def reject(self, confirmation_id: str) -> bool:
        return self._pending.pop(confirmation_id, None) is not None
