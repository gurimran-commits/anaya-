
from dataclasses import dataclass
from datetime import datetime
from threading import RLock

from app.security.confirmation import ConfirmationManager


@dataclass(frozen=True)
class ApprovalRequest:
    confirmation_id: str
    action: str
    arguments: tuple[str, ...]
    message: str
    expires_at: datetime


@dataclass(frozen=True)
class _Authorization:
    action: str
    arguments: tuple[str, ...]
    expires_at: datetime


class ApprovalService:
    """Manage bounded, expiring requests and one-time authorizations."""

    def __init__(
        self,
        confirmations: ConfirmationManager | None = None,
        max_pending: int = 1000,
        max_authorizations: int = 1000,
    ) -> None:
        if (
            not isinstance(max_pending, int)
            or isinstance(max_pending, bool)
            or max_pending <= 0
        ):
            raise ValueError("max_pending must be a positive integer")

        if (
            not isinstance(max_authorizations, int)
            or isinstance(max_authorizations, bool)
            or max_authorizations <= 0
        ):
            raise ValueError(
                "max_authorizations must be a positive integer"
            )

        self._confirmations = confirmations or ConfirmationManager()
        self._max_pending = max_pending
        self._max_authorizations = max_authorizations
        self._requests: dict[str, ApprovalRequest] = {}
        self._authorizations: dict[str, _Authorization] = {}
        self._lock = RLock()

    def _cleanup_locked(self) -> None:
        """Remove expired state. Caller must hold the service lock."""
        for confirmation_id, request in list(self._requests.items()):
            if self._confirmations.is_expired(request.expires_at):
                self._requests.pop(confirmation_id, None)
                self._confirmations.reject(confirmation_id)

        for confirmation_id, authorization in list(
            self._authorizations.items()
        ):
            if self._confirmations.is_expired(
                authorization.expires_at
            ):
                self._authorizations.pop(confirmation_id, None)

    def request_approval(
        self,
        action: str,
        arguments: tuple[str, ...] = (),
    ) -> ApprovalRequest:
        with self._lock:
            self._cleanup_locked()

            if len(self._requests) >= self._max_pending:
                raise ValueError("Too many pending approval requests")

            pending = self._confirmations.request(action, arguments)

            request = ApprovalRequest(
                confirmation_id=pending.confirmation_id,
                action=pending.action,
                arguments=pending.arguments,
                message=(
                    f"Approve action '{pending.action}' "
                    f"with arguments {pending.arguments!r}?"
                ),
                expires_at=pending.expires_at,
            )
            self._requests[request.confirmation_id] = request
            return request

    def get_request(
        self,
        confirmation_id: str,
    ) -> ApprovalRequest | None:
        """Return a request only while it remains valid."""
        with self._lock:
            self._cleanup_locked()
            return self._requests.get(confirmation_id)

    def approve(self, request: ApprovalRequest) -> bool:
        """Approve an exact pending request and create one-time authority."""
        with self._lock:
            self._cleanup_locked()

            original = self._requests.get(request.confirmation_id)
            if original is None or original != request:
                return False

            if len(self._authorizations) >= self._max_authorizations:
                return False

            pending = self._confirmations.consume_if_matches(
                request.confirmation_id,
                request.action,
                request.arguments,
            )

            self._requests.pop(request.confirmation_id, None)

            if pending is None:
                return False

            self._authorizations[request.confirmation_id] = _Authorization(
                action=pending.action,
                arguments=pending.arguments,
                expires_at=pending.expires_at,
            )
            return True

    def reject(self, request: ApprovalRequest) -> bool:
        """Reject the original request and invalidate it."""
        with self._lock:
            self._cleanup_locked()

            original = self._requests.get(request.confirmation_id)
            if original is None or original != request:
                return False

            self._requests.pop(request.confirmation_id, None)
            return self._confirmations.reject(request.confirmation_id)

    def consume_authorization(
        self,
        confirmation_id: str,
        action: str,
        arguments: tuple[str, ...] = (),
    ) -> bool:
        """Consume authorization once, for the exact approved action."""
        with self._lock:
            self._cleanup_locked()

            authorization = self._authorizations.pop(
                confirmation_id,
                None,
            )
            if authorization is None:
                return False

            return (
                authorization.action == action
                and authorization.arguments == arguments
            )
