
import re

from app.commands.executor import CommandExecutor
from app.commands.registry import CommandRegistry
from app.core.action_result import ActionResult
from app.brain.intent_handler import IntentHandler


_CONFIRMATION_PATTERN = re.compile(r"^confirm ([A-Za-z0-9_-]+)$")


class TextAssistant:
    """Route explicit text commands through the secured executor."""

    def __init__(
        self,
        registry: CommandRegistry,
        executor: CommandExecutor,
    ) -> None:
        self._registry = registry
        self._executor = executor
        self._intent_handler = IntentHandler(registry)

    def process(self, text: str) -> ActionResult:
        if not isinstance(text, str):
            return ActionResult(False, "Please enter a text command.")

        text = text.strip()

        if not text:
            return ActionResult(False, "Please enter a command.")

        if text.casefold() in {"help", "commands"}:
            names = sorted(
                command.name
                for command in self._registry.list_commands()
            )
            available = ", ".join(names) if names else "No commands registered."
            return ActionResult(
                True,
                f"Available commands: {available}. "
                "Use the exact command name followed by its arguments.",
            )

        confirmation_match = _CONFIRMATION_PATTERN.fullmatch(text)
        if confirmation_match:
            return self._confirm(confirmation_match.group(1))

        intent = self._intent_handler.parse(text)
        if intent is None:
            return ActionResult(
                False,
                "Command not recognized or its arguments are invalid. "
                "Type 'help' to see registered commands.",
            )

        result = self._executor.execute(
            intent.name,
            intent.arguments,
        )

        if result.message.startswith("Confirmation required. Confirmation ID: "):
            return ActionResult(
                False,
                result.message + " Type 'confirm ID' to approve this exact action.",
            )

        return result

    def _confirm(self, confirmation_id: str) -> ActionResult:
        service = self._executor.approval_service
        request = service.get_request(confirmation_id)

        if request is None:
            return ActionResult(
                False,
                "No valid pending approval exists for that ID.",
            )

        if not service.approve(request):
            return ActionResult(
                False,
                "Approval failed or the request has expired.",
            )

        return self._executor.execute(
            request.action,
            request.arguments,
            confirmation_id=confirmation_id,
        )
