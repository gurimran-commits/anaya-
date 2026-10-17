
import pytest

from app.commands.registry import CommandRegistry, CommandSpec


def sample_handler():
    return "ok"


# Original registry tests

def test_register_and_get_command():
    registry = CommandRegistry()
    command = CommandSpec(
        name="greet",
        description="Greet the user",
        handler=sample_handler,
    )

    registry.register(command)

    assert registry.get("greet") is command


def test_unknown_command_returns_none():
    registry = CommandRegistry()

    assert registry.get("shutdown") is None


def test_duplicate_command_is_rejected():
    registry = CommandRegistry()
    command = CommandSpec("greet", "Greet the user", sample_handler)
    registry.register(command)

    with pytest.raises(ValueError):
        registry.register(command)


def test_empty_command_name_is_rejected():
    registry = CommandRegistry()
    command = CommandSpec("", "Invalid command", sample_handler)

    with pytest.raises(ValueError):
        registry.register(command)


def test_surrounding_spaces_are_rejected():
    registry = CommandRegistry()
    command = CommandSpec(" greet ", "Invalid command", sample_handler)

    with pytest.raises(ValueError):
        registry.register(command)


def test_list_commands_returns_registered_commands():
    registry = CommandRegistry()
    command = CommandSpec("greet", "Greet the user", sample_handler)
    registry.register(command)

    assert registry.list_commands() == (command,)


# Argument-validation tests

def test_valid_arguments_are_accepted():
    registry = CommandRegistry()
    command = CommandSpec(
        name="open_app",
        description="Open an application",
        handler=sample_handler,
        validate_arguments=lambda args: (
            len(args) == 1 and args[0] in ("notepad", "calculator")
        ),
    )
    registry.register(command)

    assert registry.validate("open_app", ("notepad",))


def test_invalid_arguments_are_rejected():
    registry = CommandRegistry()
    command = CommandSpec(
        name="open_app",
        description="Open an application",
        handler=sample_handler,
        validate_arguments=lambda args: (
            len(args) == 1 and args[0] in ("notepad", "calculator")
        ),
    )
    registry.register(command)

    assert not registry.validate("open_app", ("arbitrary-command",))


def test_unknown_command_cannot_be_validated():
    registry = CommandRegistry()

    assert not registry.validate("unknown_command")


def test_non_string_arguments_are_rejected():
    registry = CommandRegistry()
    command = CommandSpec(
        name="greet",
        description="Greet the user",
        handler=sample_handler,
    )
    registry.register(command)

    assert not registry.validate("greet", (123,))


def test_validator_exception_fails_closed():
    def broken_validator(arguments):
        raise RuntimeError("Validator failed")

    registry = CommandRegistry()
    command = CommandSpec(
        name="greet",
        description="Greet the user",
        handler=sample_handler,
        validate_arguments=broken_validator,
    )
    registry.register(command)

    assert not registry.validate("greet")
