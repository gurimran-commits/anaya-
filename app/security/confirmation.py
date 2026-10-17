from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import RLock
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
        self._lock = RLock()

    def request(
        self,
        action: str,
        arguments: tuple[str, ...] = (),
    ) -> PendingConfirmation:
        if not isinstance(action, str) or not action.strip():
            raise ValueError("Action must not be empty")

        if not isinstance(arguments, tuple) or not all(
            isinstance(argument, str) for argument in arguments
        ):
            raise ValueError("Arguments must be a tuple of strings")

        confirmation = PendingConfirmation(
            confirmation_id=str(uuid4()),
            action=action,
            arguments=arguments,
            expires_at=self._clock()
            + timedelta(seconds=self._expiry_seconds),
        )

        with self._lock:
            self._pending[confirmation.confirmation_id] = confirmation

        return confirmation

    def consume_if_matches(
        self,
        confirmation_id: str,
        action: str,
        arguments: tuple[str, ...] = (),
    ) -> PendingConfirmation | None:
        """Consume a request once and return it only if it matches."""
        with self._lock:
            pending = self._pending.pop(confirmation_id, None)

            if pending is None:
                return None

            if self._clock() >= pending.expires_at:
                return None

            if pending.action != action:
                return None

            if pending.arguments != arguments:
                return None

            return pending

    def approve(
        self,
        confirmation_id: str,
        action: str,
        arguments: tuple[str, ...] = (),
    ) -> bool:
        """Validate and consume a matching confirmation request."""
        return (
            self.consume_if_matches(
                confirmation_id,
                action,
                arguments,
            )
            is not None
        )

    def reject(self, confirmation_id: str) -> bool:
        """Remove a pending request without approving it."""
        with self._lock:
            return self._pending.pop(confirmation_id, None) is not None

    def is_expired(self, expires_at: datetime) -> bool:
        """Check expiry using the manager's configured clock."""
        return self._clock() >= expires_at