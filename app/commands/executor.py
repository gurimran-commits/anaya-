
from app.core.action_result import ActionResult
from app.commands.registry import CommandRegistry
from app.security.confirmation import ConfirmationManager


class CommandExecutor:
    """Execute registered commands with validation and confirmation."""

    def __init__(
        self,
        registry: CommandRegistry,
        confirmations: ConfirmationManager | None = None,
    ) -> None:
        self._registry = registry
        self._confirmations = confirmations or ConfirmationManager()

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
                pending = self._confirmations.request(name, arguments)
                return ActionResult(
                    success=False,
                    message=(
                        "Confirmation required. "
                        f"Confirmation ID: {pending.confirmation_id}"
                    ),
                )

            if not self._confirmations.approve(
                confirmation_id,
                name,
                arguments,
            ):
                return ActionResult(
                    success=False,
                    message="Confirmation is invalid, expired, or already used.",
                )

        try:
            result = command.handler(*arguments)
            return ActionResult(success=True, message=str(result))
        except Exception:
            return ActionResult(
                success=False,
                message="Command execution failed.",
            )
