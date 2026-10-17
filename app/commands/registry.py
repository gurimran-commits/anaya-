
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class CommandSpec:
    name: str
    description: str
    handler: Callable[..., object]
    requires_confirmation: bool = False
    validate_arguments: Callable[[tuple[str, ...]], bool] = (
        lambda arguments: True
    )


class CommandRegistry:
    """Store and validate explicitly registered commands."""

    def __init__(self) -> None:
        self._commands: dict[str, CommandSpec] = {}

    def register(self, command: CommandSpec) -> None:
        name = command.name

        if not name.strip():
            raise ValueError("Command name must not be empty")

        if name != name.strip():
            raise ValueError(
                "Command name must not contain surrounding spaces"
            )

        if name in self._commands:
            raise ValueError(f"Command already registered: {name}")

        self._commands[name] = command

    def get(self, name: str) -> CommandSpec | None:
        return self._commands.get(name)

    def validate(
        self,
        name: str,
        arguments: tuple[str, ...] = (),
    ) -> bool:
        command = self.get(name)

        if command is None:
            return False

        if not isinstance(arguments, tuple):
            return False

        if not all(isinstance(arg, str) for arg in arguments):
            return False

        try:
            return command.validate_arguments(arguments) is True
        except Exception:
            return False

    def list_commands(self) -> tuple[CommandSpec, ...]:
        return tuple(self._commands.values())
