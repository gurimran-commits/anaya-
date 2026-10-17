
from dataclasses import dataclass

from app.security.confirmation import ConfirmationManager


@dataclass(frozen=True)
class ApprovalRequest:
    confirmation_id: str
    action: str
    arguments: tuple[str, ...]
    message: str


class ApprovalService:
    """Manage requests for explicit approval of sensitive actions."""

    def __init__(
        self,
        confirmations: ConfirmationManager | None = None,
    ) -> None:
        self._confirmations = confirmations or ConfirmationManager()

    def request_approval(
        self,
        action: str,
        arguments: tuple[str, ...] = (),
    ) -> ApprovalRequest:
        pending = self._confirmations.request(action, arguments)

        return ApprovalRequest(
            confirmation_id=pending.confirmation_id,
            action=pending.action,
            arguments=pending.arguments,
            message=(
                f"Approve action '{pending.action}' "
                f"with arguments {pending.arguments!r}?"
            ),
        )

    def approve(self, request: ApprovalRequest) -> bool:
        return self._confirmations.approve(
            request.confirmation_id,
            request.action,
            request.arguments,
        )

    def reject(self, request: ApprovalRequest) -> bool:
        return self._confirmations.reject(request.confirmation_id)
