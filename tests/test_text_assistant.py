
from app.brain.intent_handler import IntentHandler
from app.brain.text_assistant import TextAssistant
from app.commands.executor import CommandExecutor
from app.commands.registry import CommandRegistry, CommandSpec


def build_assistant():
    registry = CommandRegistry()
    registry.register(
        CommandSpec(
            name="demo.echo",
            description="Return supplied text",
            handler=lambda value: value,
            validate_arguments=lambda args: len(args) == 1,
        )
    )
    registry.register(
        CommandSpec(
            name="demo.delete",
            description="Test a protected action",
            handler=lambda value: f"Deleted {value}",
            requires_confirmation=True,
            validate_arguments=lambda args: len(args) == 1,
        )
    )
    executor = CommandExecutor(registry)
    return registry, executor, TextAssistant(registry, executor)


def test_intent_handler_parses_registered_command():
    registry, _, _ = build_assistant()
    intent = IntentHandler(registry).parse('demo.echo "hello world"')

    assert intent is not None
    assert intent.name == "demo.echo"
    assert intent.arguments == ("hello world",)


def test_unknown_command_is_not_executed():
    _, _, assistant = build_assistant()

    result = assistant.process("system.run something")

    assert not result.success
    assert "not recognized" in result.message


def test_invalid_arguments_are_rejected():
    _, _, assistant = build_assistant()

    result = assistant.process("demo.echo")

    assert not result.success
    assert "arguments are invalid" in result.message


def test_registered_command_executes():
    _, _, assistant = build_assistant()

    result = assistant.process('demo.echo "hello world"')

    assert result.success
    assert result.message == "hello world"


def test_sensitive_command_requires_confirmation():
    _, _, assistant = build_assistant()

    result = assistant.process("demo.delete report.txt")

    assert not result.success
    assert "Confirmation required" in result.message
    assert "confirm " in result.message


def test_confirmation_executes_exact_approved_action():
    _, _, assistant = build_assistant()

    request_result = assistant.process("demo.delete report.txt")
    confirmation_id = request_result.message.split("Confirmation ID: ")[1].split()[0]

    result = assistant.process(f"confirm {confirmation_id}")

    assert result.success
    assert result.message == "Deleted report.txt"


def test_help_lists_registered_commands():
    _, _, assistant = build_assistant()

    result = assistant.process("help")

    assert result.success
    assert "demo.echo" in result.message
    assert "demo.delete" in result.message
