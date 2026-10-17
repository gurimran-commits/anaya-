from app.core.action_result import ActionResult
from app.commands.registry import CommandRegistry
from app.security.approval_service import ApprovalService


class CommandExecutor:
    """Execute registered commands with explicit approval enforcement."""

    def __init__(
        self,
        registry: CommandRegistry,
        approval_service: ApprovalService | None = None,
    ) -> None:
        self._registry = registry
        self._approval_service = approval_service or ApprovalService()

    @property
    def approval_service(self) -> ApprovalService:
        """Expose the service for integration with the application UI."""
        return self._approval_service

    def execute(
        self,
        name: str,
        arguments: tuple[str, ...] = (),
        confirmation_id: str | None = None,
    ) -> ActionResult:
        command = self._registry.get(name)

        if command is None:
            return ActionResult(
                success=False,
                message=f"Unknown command: {name}",
            )

        if not self._registry.validate(name, arguments):
            return ActionResult(
                success=False,
                message="Command arguments are invalid.",
            )

        if command.requires_confirmation:
            if confirmation_id is None:
                try:
                    request = self._approval_service.request_approval(
                        name,
                        arguments,
                    )
                except (TypeError, ValueError):
                    return ActionResult(
                        success=False,
                        message="Could not create an approval request.",
                    )

                return ActionResult(
                    success=False,
                    message=(
                        "Confirmation required. "
                        f"Confirmation ID: {request.confirmation_id}"
                    ),
                )

            if not self._approval_service.consume_authorization(
                confirmation_id,
                name,
                arguments,
            ):
                return ActionResult(
                    success=False,
                    message=(
                        "Approval is invalid, expired, mismatched, "
                        "or already used."
                    ),
                )

        try:
            result = command.handler(*arguments)
            return ActionResult(
                success=True,
                message=str(result),
            )
        except Exception:
            return ActionResult(
                success=False,
                message="Command execution failed.",
            )