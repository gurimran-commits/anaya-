from app.brain.context import ConversationContext
from app.brain.intent_handler import IntentHandler
from app.brain.models import IntentStatus
from app.commands.executor import CommandExecutor
from app.commands.registry import CommandRegistry
from app.core.action_result import ActionResult


class TextAssistant:
    """Process natural language and text commands without bypassing security controls."""

    def __init__(
        self,
        registry: CommandRegistry,
        executor: CommandExecutor,
    ) -> None:
        self._registry = registry
        self._executor = executor
        self._intent_handler = IntentHandler(registry)
        self._context = ConversationContext()

    @property
    def context(self) -> ConversationContext:
        """Expose current conversation context for inspection and testing."""
        return self._context

    def reset_context(self) -> None:
        """Reset conversational context and clear any pending clarification."""
        self._context.reset()

    def process(self, text: str) -> ActionResult:
        """Process user text input, parse intent, and execute registered operations."""
        if not isinstance(text, str):
            return ActionResult(False, "Please enter a text command.")

        text = text.strip()
        if not text:
            return ActionResult(False, "Please enter a command.")

        casefolded = text.casefold()

        if casefolded in {"help", "commands"}:
            names = sorted(
                command.name for command in self._registry.list_commands()
            )
            available = (
                ", ".join(names) if names else "No commands registered."
            )
            return ActionResult(
                True,
                f"Available commands: {available}. "
                "Use the exact command name followed by its arguments.",
            )

        # Enforce security barrier: text confirmation is strictly forbidden.
        if (
            casefolded.startswith("confirm ")
            or casefolded.startswith("approve ")
            or casefolded in {"confirm", "approve", "yes"}
        ):
            return ActionResult(
                False,
                "Text confirmation is disabled. Approve or reject the "
                "pending request through the application's explicit "
                "approval interface.",
            )

        # Handle direct context reset command.
        if casefolded in {"reset", "reset context", "clear context", "clear history"}:
            self.reset_context()
            return ActionResult(True, "Context has been reset.")

        resolution = self._intent_handler.resolve(text, self._context)

        if resolution.status == IntentStatus.CANCELLED:
            self._context.clear_pending()
            return ActionResult(False, resolution.message or "Action cancelled.")

        if resolution.status == IntentStatus.RESET_CONTEXT:
            self.reset_context()
            return ActionResult(True, resolution.message or "Context has been reset.")

        if resolution.status in {
            IntentStatus.MISSING_ARGUMENT,
            IntentStatus.AMBIGUOUS,
            IntentStatus.UNSUPPORTED,
        }:
            return ActionResult(
                False,
                resolution.message
                or (
                    "Command not recognized or its arguments are invalid. "
                    "Type 'help' to see registered commands."
                ),
            )

        if resolution.status == IntentStatus.INVALID:
            msg = resolution.message or (
                "Command not recognized or its arguments are invalid. "
                "Type 'help' to see registered commands."
            )
            if "arguments are invalid" in msg and "help" not in msg:
                msg = f"{msg} Type 'help' to see registered commands."
            return ActionResult(False, msg)

        if resolution.status == IntentStatus.VALID and resolution.intent is not None:
            intent = resolution.intent
            result = self._executor.execute(
                intent.name,
                intent.arguments,
            )

            if result.message.startswith("Confirmation required. Confirmation ID: "):
                # Record target in context so user can reference it in follow-ups.
                if resolution.target:
                    self._context.update(
                        intent.name,
                        target=resolution.target,
                        directory_index=resolution.directory_index,
                    )
                return ActionResult(
                    False,
                    result.message
                    + " Approve or reject this request through the "
                    "application's explicit approval interface.",
                )

            if result.success:
                self._context.update(
                    intent.name,
                    target=resolution.target,
                    directory_index=resolution.directory_index,
                )

            return result

        return ActionResult(
            False,
            "Command not recognized or its arguments are invalid. "
            "Type 'help' to see registered commands.",
        )
