
from app.commands.registry import CommandRegistry, CommandSpec
from app.commands.executor import CommandExecutor
from app.security.confirmation import ConfirmationManager

def test_registered_command_executes():
    registry = CommandRegistry()
    registry.register(
        CommandSpec(
            name="greet",
            description="Greet the user",
            handler=lambda name: f"Hello, {name}!",
        )
    )
    executor = CommandExecutor(registry)

    result = executor.execute("greet", ("Anaya",))

    assert result.success is True
    assert result.message == "Hello, Anaya!"


def test_unknown_command_is_rejected():
    executor = CommandExecutor(CommandRegistry())

    result = executor.execute("shutdown")

    assert result.success is False
    assert result.message == "Unknown command: shutdown"


def test_invalid_arguments_prevent_execution():
    calls = []
    registry = CommandRegistry()
    registry.register(
        CommandSpec(
            name="greet",
            description="Greet the user",
            handler=lambda name: calls.append(name),
            validate_arguments=lambda args: (
                len(args) == 1 and args[0] == "Anaya"
            ),
        )
    )
    executor = CommandExecutor(registry)

    result = executor.execute("greet", ("unexpected",))

    assert result.success is False
    assert calls == []


def test_handler_exception_returns_failure():
    def broken_handler():
        raise RuntimeError("Internal details")

    registry = CommandRegistry()
    registry.register(
        CommandSpec(
            name="test_failure",
            description="Test failure handling",
            handler=broken_handler,
        )
    )
    executor = CommandExecutor(registry)

    result = executor.execute("test_failure")

    assert result.success is False
    assert result.message == "Command execution failed."


def test_sensitive_command_requires_confirmation():
    calls = []
    registry = CommandRegistry()
    registry.register(
        CommandSpec(
            name="shutdown",
            description="Test sensitive command",
            handler=lambda: calls.append("executed"),
            requires_confirmation=True,
        )
    )
    executor = CommandExecutor(registry)

    result = executor.execute("shutdown")

    assert result.success is False
    assert "Confirmation required" in result.message
    assert calls == []


def test_sensitive_command_executes_after_matching_confirmation():
    calls = []
    registry = CommandRegistry()
    registry.register(
        CommandSpec(
            name="shutdown",
            description="Test sensitive command",
            handler=lambda: calls.append("executed"),
            requires_confirmation=True,
        )
    )
    confirmations = ConfirmationManager()
    executor = CommandExecutor(registry, confirmations)

    request = confirmations.request("shutdown", ())
    result = executor.execute(
        "shutdown",
        confirmation_id=request.confirmation_id,
    )

    assert result.success is True
    assert calls == ["executed"]


def test_sensitive_command_rejects_confirmation_for_different_arguments():
    calls = []
    registry = CommandRegistry()
    registry.register(
        CommandSpec(
            name="delete_file",
            description="Test sensitive command",
            handler=lambda filename: calls.append(filename),
            validate_arguments=lambda args: len(args) == 1,
            requires_confirmation=True,
        )
    )
    confirmations = ConfirmationManager()
    executor = CommandExecutor(registry, confirmations)

    request = confirmations.request("delete_file", ("notes.txt",))
    result = executor.execute(
        "delete_file",
        ("important.txt",),
        confirmation_id=request.confirmation_id,
    )

    assert result.success is False
    assert calls == []
