
import pytest

from app.commands.executor import CommandExecutor
from app.commands.filesystem import FilesystemService
from app.commands.filesystem_commands import register_filesystem_commands
from app.commands.registry import CommandRegistry


@pytest.fixture
def setup_commands(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()

    filesystem = FilesystemService((allowed,))
    registry = CommandRegistry()
    register_filesystem_commands(registry, filesystem)
    executor = CommandExecutor(registry)

    return executor, allowed


def test_registers_filesystem_commands(setup_commands):
    executor, _ = setup_commands

    assert executor._registry.get("files.list") is not None
    assert executor._registry.get("files.read") is not None
    assert executor._registry.get("files.create") is not None
    assert executor._registry.get("files.delete") is not None


def test_create_and_read_file(setup_commands):
    executor, allowed = setup_commands

    result = executor.execute(
        "files.create",
        ("0", "notes.txt", "hello Anaya"),
    )
    assert result.success
    assert (allowed / "notes.txt").read_text(encoding="utf-8") == "hello Anaya"

    result = executor.execute("files.read", ("0", "notes.txt"))
    assert result.success
    assert result.message == "hello Anaya"


def test_delete_requires_approval(setup_commands):
    executor, allowed = setup_commands
    target = allowed / "old.txt"
    target.write_text("temporary", encoding="utf-8")

    result = executor.execute("files.delete", ("0", "old.txt"))

    assert not result.success
    assert "Confirmation required" in result.message
    assert target.exists()


def test_invalid_path_is_rejected(setup_commands):
    executor, _ = setup_commands

    result = executor.execute("files.read", ("0", "../outside.txt"))

    assert not result.success
    assert result.message == "Command arguments are invalid."

def test_delete_succeeds_after_explicit_approval(setup_commands):
    executor, allowed = setup_commands
    target = allowed / "old.txt"
    target.write_text("temporary", encoding="utf-8")

    result = executor.execute("files.delete", ("0", "old.txt"))
    assert not result.success
    assert "Confirmation ID:" in result.message
    assert target.exists()

    confirmation_id = result.message.split("Confirmation ID: ", 1)[1]
    approval = executor.approval_service
    request = approval.get_request(confirmation_id)

    assert request is not None
    assert request.action == "files.delete"
    assert request.arguments == ("0", "old.txt")
    assert approval.approve(request)

    result = executor.execute(
        "files.delete",
        ("0", "old.txt"),
        confirmation_id=confirmation_id,
    )

    assert result.success
    assert not target.exists()


def test_delete_approval_cannot_be_reused(setup_commands):
    executor, allowed = setup_commands
    target = allowed / "old.txt"
    target.write_text("temporary", encoding="utf-8")

    result = executor.execute("files.delete", ("0", "old.txt"))
    confirmation_id = result.message.split("Confirmation ID: ", 1)[1]

    approval = executor.approval_service
    request = approval.get_request(confirmation_id)
    assert request is not None
    assert approval.approve(request)

    first = executor.execute(
        "files.delete",
        ("0", "old.txt"),
        confirmation_id=confirmation_id,
    )
    assert first.success
    assert not target.exists()

    second = executor.execute(
        "files.delete",
        ("0", "old.txt"),
        confirmation_id=confirmation_id,
    )
    assert not second.success
    assert "invalid, expired, mismatched" in second.message


def test_delete_approval_cannot_authorize_another_file(setup_commands):
    executor, allowed = setup_commands
    original = allowed / "old.txt"
    other = allowed / "important.txt"
    original.write_text("temporary", encoding="utf-8")
    other.write_text("keep this", encoding="utf-8")

    result = executor.execute("files.delete", ("0", "old.txt"))
    confirmation_id = result.message.split("Confirmation ID: ", 1)[1]

    approval = executor.approval_service
    request = approval.get_request(confirmation_id)
    assert request is not None
    assert approval.approve(request)

    result = executor.execute(
        "files.delete",
        ("0", "important.txt"),
        confirmation_id=confirmation_id,
    )

    assert not result.success
    assert original.exists()
    assert other.exists()
