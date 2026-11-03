import shlex

from app.brain.context import ConversationContext
from app.brain.models import CommandIntent, IntentResolution, IntentStatus
from app.brain.natural_parser import NaturalLanguageParser
from app.commands.registry import CommandRegistry


class IntentHandler:
    """Parse text into validated command intents using exact and natural language parsing."""

    MAX_INPUT_LENGTH = 4096

    def __init__(self, registry: CommandRegistry) -> None:
        self._registry = registry
        self._natural_parser = NaturalLanguageParser()

    def parse(self, text: str) -> CommandIntent | None:
        """Parse input text into a CommandIntent if valid.

        Maintains backward compatibility with explicit command syntax.
        """
        resolution = self.resolve(text, ConversationContext())
        if resolution.status == IntentStatus.VALID:
            return resolution.intent
        return None

    def resolve(
        self,
        text: str,
        context: ConversationContext | None = None,
    ) -> IntentResolution:
        """Resolve text into a structured IntentResolution with status and arguments."""
        if not isinstance(text, str):
            return IntentResolution(
                status=IntentStatus.INVALID,
                message="Please enter a text command.",
            )

        text = text.strip()
        if not text:
            return IntentResolution(
                status=IntentStatus.INVALID,
                message="Please enter a command.",
            )

        if len(text) > self.MAX_INPUT_LENGTH:
            return IntentResolution(
                status=IntentStatus.INVALID,
                message=f"Input exceeds maximum permitted length of {self.MAX_INPUT_LENGTH} characters.",
            )

        active_context = context if context is not None else ConversationContext()

        # 1. Attempt exact command matching via shlex.
        try:
            parts = shlex.split(text)
        except ValueError:
            parts = None

        if parts:
            name, *arguments = parts
            command_spec = self._registry.get(name)
            if command_spec is not None:
                tuple_args = tuple(arguments)
                if self._registry.validate(name, tuple_args):
                    target = tuple_args[1] if len(tuple_args) >= 2 else (tuple_args[0] if tuple_args else None)
                    dir_idx = tuple_args[0] if (name.startswith("files.") and tuple_args and tuple_args[0].isdigit()) else "0"
                    return IntentResolution(
                        status=IntentStatus.VALID,
                        intent=CommandIntent(name, tuple_args),
                        target=target,
                        directory_index=dir_idx,
                    )
                return IntentResolution(
                    status=IntentStatus.INVALID,
                    message="Command arguments are invalid.",
                )

        # 2. Attempt natural language parsing.
        resolution = self._natural_parser.parse(
            text, active_context, self._registry
        )

        if resolution.status == IntentStatus.VALID and resolution.intent is not None:
            if not self._registry.validate(
                resolution.intent.name, resolution.intent.arguments
            ):
                return IntentResolution(
                    status=IntentStatus.INVALID,
                    message="Command arguments are invalid.",
                )

        return resolution
