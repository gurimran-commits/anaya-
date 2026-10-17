
import shlex
from dataclasses import dataclass

from app.commands.registry import CommandRegistry


@dataclass(frozen=True)
class CommandIntent:
    name: str
    arguments: tuple[str, ...]


class IntentHandler:
    """Parse explicit text commands without executing them."""

    MAX_INPUT_LENGTH = 4096

    def __init__(self, registry: CommandRegistry) -> None:
        self._registry = registry

    def parse(self, text: str) -> CommandIntent | None:
        if not isinstance(text, str):
            return None

        text = text.strip()
        if not text or len(text) > self.MAX_INPUT_LENGTH:
            return None

        try:
            parts = shlex.split(text)
        except ValueError:
            return None

        if not parts:
            return None

        name, *arguments = parts
        intent = CommandIntent(name, tuple(arguments))

        if not self._registry.validate(
            intent.name, intent.arguments
        ):
            return None

        return intent
