
from app.brain.intent_handler import IntentHandler
from app.commands.executor import CommandExecutor
from app.commands.registry import CommandRegistry
from app.core.action_result import ActionResult


class TextAssistant:
    """Process text commands without implicitly approving sensitive actions."""

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
            available = (
                ", ".join(names) if names else "No commands registered."
            )
            return ActionResult(
                True,
                f"Available commands: {available}. "
                "Use the exact command name followed by its arguments.",
            )

        if text.casefold().startswith("confirm "):
            return ActionResult(
                False,
                "Text confirmation is disabled. Approve or reject the "
                "pending request through the application's explicit "
                "approval interface.",
            )

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

        if result.message.startswith(
            "Confirmation required. Confirmation ID: "
        ):
            return ActionResult(
                False,
                result.message
                + " Approve or reject this request through the "
                "application's explicit approval interface.",
            )

        return result
