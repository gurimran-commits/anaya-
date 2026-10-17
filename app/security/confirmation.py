
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
    """Create and validate short-lived, one-time confirmations."""

    def __init__(
        self,
        expiry_seconds: int = 60,
        clock: Callable[[], datetime] | None = None,
        max_pending: int = 1000,
    ) -> None:
        if (
            not isinstance(expiry_seconds, int)
            or isinstance(expiry_seconds, bool)
            or expiry_seconds <= 0
        ):
            raise ValueError("expiry_seconds must be a positive integer")

        if (
            not isinstance(max_pending, int)
            or isinstance(max_pending, bool)
            or max_pending <= 0
        ):
            raise ValueError("max_pending must be a positive integer")

        self._expiry_seconds = expiry_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._max_pending = max_pending
        self._pending: dict[str, PendingConfirmation] = {}
        self._lock = RLock()

    def _remove_expired_locked(self) -> None:
        now = self._clock()
        expired_ids = [
            confirmation_id
            for confirmation_id, request in self._pending.items()
            if now >= request.expires_at
        ]
        for confirmation_id in expired_ids:
            self._pending.pop(confirmation_id, None)

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

        with self._lock:
            self._remove_expired_locked()

            if len(self._pending) >= self._max_pending:
                raise ValueError("Too many pending confirmations")

            confirmation = PendingConfirmation(
                confirmation_id=str(uuid4()),
                action=action,
                arguments=arguments,
                expires_at=self._clock()
                + timedelta(seconds=self._expiry_seconds),
            )
            self._pending[confirmation.confirmation_id] = confirmation
            return confirmation

    def get(self, confirmation_id: str) -> PendingConfirmation | None:
        with self._lock:
            self._remove_expired_locked()
            return self._pending.get(confirmation_id)

    def consume_if_matches(
        self,
        confirmation_id: str,
        action: str,
        arguments: tuple[str, ...] = (),
    ) -> PendingConfirmation | None:
        with self._lock:
            pending = self._pending.pop(confirmation_id, None)

            if pending is None or self._clock() >= pending.expires_at:
                return None

            if pending.action != action or pending.arguments != arguments:
                return None

            return pending

    def approve(
        self,
        confirmation_id: str,
        action: str,
        arguments: tuple[str, ...] = (),
    ) -> bool:
        """Validate and consume a matching confirmation request."""
        return self.consume_if_matches(
            confirmation_id,
            action,
            arguments,
        ) is not None

    def reject(self, confirmation_id: str) -> bool:
        """Remove a pending request without approving it."""
        with self._lock:
            return self._pending.pop(confirmation_id, None) is not None

    def is_expired(self, expires_at: datetime) -> bool:
        return self._clock() >= expires_at
