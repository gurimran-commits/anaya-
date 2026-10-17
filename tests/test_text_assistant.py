
from app.brain.intent_handler import IntentHandler
from app.brain.text_assistant import TextAssistant
from app.commands.executor import CommandExecutor
from app.commands.registry import CommandRegistry, CommandSpec


def build_assistant():
    registry = CommandRegistry()
    executed = []

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
            handler=lambda value: executed.append(value) or f"Deleted {value}",
            requires_confirmation=True,
            validate_arguments=lambda args: len(args) == 1,
        )
    )

    executor = CommandExecutor(registry)
    assistant = TextAssistant(registry, executor)
    return registry, executor, assistant, executed


def test_intent_handler_parses_registered_command():
    registry, _, _, _ = build_assistant()
    intent = IntentHandler(registry).parse('demo.echo "hello world"')

    assert intent is not None
    assert intent.name == "demo.echo"
    assert intent.arguments == ("hello world",)


def test_unknown_command_is_not_executed():
    _, _, assistant, executed = build_assistant()

    result = assistant.process("system.run something")

    assert not result.success
    assert "not recognized" in result.message
    assert executed == []


def test_invalid_arguments_are_rejected():
    _, _, assistant, _ = build_assistant()

    result = assistant.process("demo.echo")

    assert not result.success
    assert "arguments are invalid" in result.message


def test_registered_command_executes():
    _, _, assistant, _ = build_assistant()

    result = assistant.process('demo.echo "hello world"')

    assert result.success
    assert result.message == "hello world"


def test_sensitive_command_requires_confirmation():
    _, _, assistant, executed = build_assistant()

    result = assistant.process("demo.delete report.txt")

    assert not result.success
    assert "Confirmation required" in result.message
    assert "Confirmation ID: " in result.message
    assert executed == []


def test_typing_confirmation_id_does_not_approve():
    _, executor, assistant, executed = build_assistant()

    request_result = assistant.process("demo.delete report.txt")
    confirmation_id = request_result.message.split("Confirmation ID: ")[1].split()[0]

    result = assistant.process(f"confirm {confirmation_id}")

    assert not result.success
    assert "Text confirmation is disabled" in result.message
    assert executed == []
    assert executor.approval_service.get_request(confirmation_id) is not None


def test_explicit_approval_executes_exact_action_once():
    _, executor, assistant, executed = build_assistant()

    request_result = assistant.process("demo.delete report.txt")
    confirmation_id = request_result.message.split("Confirmation ID: ")[1].split()[0]

    service = executor.approval_service
    request = service.get_request(confirmation_id)

    assert request is not None
    assert service.approve(request)

    result = executor.execute(
        request.action,
        request.arguments,
        confirmation_id=confirmation_id,
    )

    assert result.success
    assert result.message == "Deleted report.txt"
    assert executed == ["report.txt"]

    replay = executor.execute(
        request.action,
        request.arguments,
        confirmation_id=confirmation_id,
    )

    assert not replay.success
    assert executed == ["report.txt"]


def test_help_lists_registered_commands():
    _, _, assistant, _ = build_assistant()

    result = assistant.process("help")

    assert result.success
    assert "demo.echo" in result.message
    assert "demo.delete" in result.message
