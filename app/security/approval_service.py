from dataclasses import dataclass
from threading import RLock

from app.security.confirmation import ConfirmationManager
from datetime import datetime

@dataclass(frozen=True)
class ApprovalRequest:
    confirmation_id: str
    action: str
    arguments: tuple[str, ...]
    message: str


@dataclass(frozen=True)
class _Authorization:
    action: str
    arguments: tuple[str, ...]
    expires_at: datetime


class ApprovalService:
    """Manage approval requests and one-time execution authorizations.

    The application UI must call approve() only after an explicit
    affirmative user action. This class alone cannot prove that a
    human actually provided consent.
    """

    def __init__(
        self,
        confirmations: ConfirmationManager | None = None,
    ) -> None:
        self._confirmations = confirmations or ConfirmationManager()
        self._requests: dict[str, ApprovalRequest] = {}
        self._authorizations: dict[str, _Authorization] = {}
        self._lock = RLock()

    def request_approval(
        self,
        action: str,
        arguments: tuple[str, ...] = (),
    ) -> ApprovalRequest:
        pending = self._confirmations.request(action, arguments)

        request = ApprovalRequest(
            confirmation_id=pending.confirmation_id,
            action=pending.action,
            arguments=pending.arguments,
            message=(
                f"Approve action '{pending.action}' "
                f"with arguments {pending.arguments!r}?"
            ),
        )

        with self._lock:
            self._requests[request.confirmation_id] = request

        return request

    def get_request(
        self,
        confirmation_id: str,
    ) -> ApprovalRequest | None:
        """Retrieve the original request for presentation in the UI."""
        with self._lock:
            return self._requests.get(confirmation_id)

    def approve(self, request: ApprovalRequest) -> bool:
        """Approve an exact pending request and create one-time authority."""
        with self._lock:
            original = self._requests.get(request.confirmation_id)

            # Do not accept altered or reconstructed request details.
            if original is None or original != request:
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
            original = self._requests.get(request.confirmation_id)

            if original is None or original != request:
                return False

            self._requests.pop(request.confirmation_id, None)

            return self._confirmations.reject(
                request.confirmation_id
            )

    def consume_authorization(
        self,
        confirmation_id: str,
        action: str,
        arguments: tuple[str, ...] = (),
    ) -> bool:
        """Consume one-time authorization for the exact approved action."""
        with self._lock:
            authorization = self._authorizations.pop(
                confirmation_id,
                None,
            )

            if authorization is None:
                return False

            if self._confirmations.is_expired(
                authorization.expires_at
            ):
                return False

            return (
                authorization.action == action
                and authorization.arguments == arguments
            )