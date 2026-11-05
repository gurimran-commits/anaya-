"""End-to-end tests for Anaya CLI application entry point (app.main.main).

Validates the full pathway from terminal I/O through AssistantController,
AnayaRuntime, and the deterministic command and security pipeline.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Callable

import pytest

from app.ai.models import (
    ChatMessage,
    GenerationOptions,
    GenerationResult,
    ModelDetails,
    ModelInfo,
)
from app.bootstrap import create_controller
from app.commands.executor import CommandExecutor
from app.controller import AssistantController
from app.main import main
from app.security.approval_service import ApprovalService
from tests.test_e2e.conftest import TempAnayaPaths


@pytest.fixture
def run_cli(
    temp_anaya_dir: TempAnayaPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    """Execute app.main.main() wired to temporary databases with full I/O tracking."""
    tracked_controllers: list[AssistantController] = []

    def _invoke(
        inputs: list[str] | Callable[[str], str],
        argv: list[str] | None = None,
        enable_ai: bool = False,
        custom_ai_provider: Any = None,
    ) -> tuple[int, list[str], list[str], AssistantController | None]:
        outputs: list[str] = []
        errors: list[str] = []

        if isinstance(inputs, list):
            input_iter = iter(inputs)

            def _in(prompt: str) -> str:
                try:
                    return next(input_iter)
                except StopIteration:
                    return "exit"

            input_fn = _in
        else:
            input_fn = inputs

        real_create = create_controller

        def _patched_create(**kwargs: Any) -> AssistantController:
            kwargs.setdefault("tasks_db_path", temp_anaya_dir.tasks_db)
            kwargs.setdefault("memory_db_path", temp_anaya_dir.memory_db)
            kwargs.setdefault("enable_ai", enable_ai)
            if custom_ai_provider is not None:
                kwargs["custom_ai_provider"] = custom_ai_provider
            ctrl = real_create(**kwargs)
            tracked_controllers.append(ctrl)
            return ctrl

        monkeypatch.setattr("app.main.create_controller", _patched_create)

        exit_code = main(
            argv=argv if argv is not None else [],
            input_fn=input_fn,
            print_fn=outputs.append,
            print_err=errors.append,
        )

        active_ctrl = tracked_controllers[-1] if tracked_controllers else None
        return exit_code, outputs, errors, active_ctrl

    yield _invoke

    for ctrl in reversed(tracked_controllers):
        if ctrl.is_running:
            try:
                ctrl.stop(timeout=2.0)
            except Exception:
                pass


def test_cli_starts_and_exits_cleanly(run_cli: Any) -> None:
    """Scenario 1: CLI starts, displays banner, and cleanly exits on 'exit', 'quit', or ':q'."""
    for exit_cmd in ["exit", "quit", ":q", "  EXIT  "]:
        exit_code, outputs, errors, ctrl = run_cli([exit_cmd])

        assert exit_code == 0
        assert not errors
        assert any("Anaya Assistant (Text Mode)" in line for line in outputs)
        assert any("Goodbye!" in line for line in outputs)
        assert ctrl is not None
        assert ctrl._cleanly_stopped is True
        assert ctrl.is_running is False


def test_cli_eof_and_interrupt_exit(run_cli: Any) -> None:
    """Scenario 1 (cont): EOFError and KeyboardInterrupt at the prompt exit gracefully with code 0."""
    # Test EOFError
    def _eof_input(prompt: str) -> str:
        raise EOFError()

    exit_code, outputs, errors, ctrl = run_cli(_eof_input)
    assert exit_code == 0
    assert any("Goodbye!" in line for line in outputs)
    assert ctrl is not None
    assert ctrl._cleanly_stopped is True

    # Test KeyboardInterrupt
    def _sigint_input(prompt: str) -> str:
        raise KeyboardInterrupt()

    exit_code2, outputs2, errors2, ctrl2 = run_cli(_sigint_input)
    assert exit_code2 == 0
    assert any("Goodbye!" in line for line in outputs2)
    assert ctrl2 is not None
    assert ctrl2._cleanly_stopped is True


def test_cli_help_behavior(run_cli: Any) -> None:
    """Scenario 2: Help behavior in the REPL and via standard command-line flags."""
    # 1. REPL 'help' command
    exit_code, outputs, errors, _ = run_cli(["help", "exit"])
    assert exit_code == 0
    assert not errors
    help_output = next(line for line in outputs if line.startswith("Available commands:"))
    assert "tasks.create" in help_output
    assert "tasks.list" in help_output
    assert "memory.save" in help_output
    assert "skills.list" in help_output

    # 2. CLI flags --help and -h (handled by argparse)
    for flag in ["--help", "-h"]:
        cli_outputs: list[str] = []
        code = main(argv=[flag], print_fn=cli_outputs.append)
        assert code == 0


def test_cli_multiple_inputs_sequential_execution(run_cli: Any) -> None:
    """Scenario 3: Input loop handles multiple commands in sequence and exits cleanly."""
    commands = [
        "tasks.list",
        "memory.list",
        "skills.list",
        "exit",
    ]
    exit_code, outputs, errors, ctrl = run_cli(commands)

    assert exit_code == 0
    assert not errors
    assert ctrl is not None
    assert ctrl._cleanly_stopped is True

    # Verify all commands produced output
    assert any("No pending tasks found." in line for line in outputs)
    assert any("No memories stored." in line for line in outputs)
    assert any("Goodbye!" in line for line in outputs)


def test_cli_empty_and_whitespace_input(run_cli: Any) -> None:
    """Scenario 4: Empty strings and whitespace-only inputs are ignored without crashing."""
    commands = [
        "",
        "   ",
        "\t",
        "\n",
        "tasks.list",
        "exit",
    ]
    exit_code, outputs, errors, _ = run_cli(commands)

    assert exit_code == 0
    assert not errors
    # After ignoring blank lines, tasks.list executes and prints result
    assert any("No pending tasks found." in line for line in outputs)
    assert any("Goodbye!" in line for line in outputs)


def test_cli_invalid_or_unsupported_input_handled_safely(run_cli: Any) -> None:
    """Scenario 5: Unrecognized commands return a helpful message without terminating the loop."""
    commands = [
        "unrecognized_command_xyz",
        "status",  # status is not a registered command
        "exit",
    ]
    exit_code, outputs, errors, ctrl = run_cli(commands)

    assert exit_code == 0
    assert not errors
    unsupported_msgs = [
        line for line in outputs
        if "Command not recognized or its arguments are invalid." in line
    ]
    assert len(unsupported_msgs) == 2
    assert ctrl is not None
    assert ctrl._cleanly_stopped is True


def test_cli_valid_deterministic_command_pipeline(
    run_cli: Any,
    temp_anaya_dir: TempAnayaPaths,
) -> None:
    """Scenario 6: Valid command flows end-to-end through real controller to SQLite disk storage."""
    commands = [
        "tasks.create Write_E2E_Tests 2026-12-01",
        "tasks.list pending",
        "exit",
    ]
    exit_code, outputs, errors, ctrl = run_cli(commands)

    assert exit_code == 0
    assert not errors

    # Check terminal output
    create_line = next(line for line in outputs if "Created task: 'Write_E2E_Tests'" in line)
    assert create_line is not None
    assert "Due: 2026-12-01" in create_line

    list_line = next(line for line in outputs if "Pending tasks:" in line)
    assert list_line is not None
    assert any("Write_E2E_Tests" in line for line in outputs)

    # Verify real data on disk in temporary SQLite database
    assert temp_anaya_dir.tasks_db.is_file()
    conn = sqlite3.connect(temp_anaya_dir.tasks_db)
    cursor = conn.cursor()
    cursor.execute("SELECT title, status FROM tasks")
    rows = cursor.fetchall()
    conn.close()

    assert len(rows) == 1
    assert rows[0][0] == "Write_E2E_Tests"
    assert rows[0][1] == "pending"


def test_cli_error_sanitization_on_fatal_startup(
    temp_anaya_dir: TempAnayaPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario 7: Fatal startup exceptions present sanitized messages to terminal without leaking paths."""
    sensitive_detail = "D:/confidential/keys/master_secret.pem"

    def _failing_create(**kwargs: Any) -> Any:
        raise RuntimeError(f"Database corrupted at {sensitive_detail}")

    monkeypatch.setattr("app.main.create_controller", _failing_create)

    outputs: list[str] = []
    errors: list[str] = []

    exit_code = main(
        argv=[],
        input_fn=lambda prompt: "exit",
        print_fn=outputs.append,
        print_err=errors.append,
    )

    assert exit_code == 1
    assert any("Fatal error (RuntimeError). Check logs for details." in err for err in errors)
    # Critical security check: sensitive path must NOT appear in terminal error output
    assert not any(sensitive_detail in err for err in errors)


def test_cli_error_sanitization_on_shutdown_failure(
    run_cli: Any,
    monkeypatch: pytest.MonkeyPatch,
    temp_anaya_dir: TempAnayaPaths,
) -> None:
    """Scenario 7 (cont): Shutdown failures report sanitized message to terminal."""
    real_create = create_controller
    sensitive_path = "C:/Windows/System32/drivers/etc/hosts"

    def _create_with_failing_stop(**kwargs: Any) -> AssistantController:
        kwargs.setdefault("tasks_db_path", temp_anaya_dir.tasks_db)
        kwargs.setdefault("memory_db_path", temp_anaya_dir.memory_db)
        kwargs.setdefault("enable_speech", False)
        kwargs.setdefault("enable_ai", False)
        ctrl = real_create(**kwargs)

        # Force stop() to raise with sensitive message while still cleanly stopping scheduler thread
        def _failing_stop(timeout: float = 5.0) -> None:
            try:
                ctrl.runtime.stop(timeout=1.0)
            except Exception:
                pass
            raise RuntimeError(f"Lock timeout on {sensitive_path}")

        ctrl.stop = _failing_stop  # type: ignore[method-assign]
        return ctrl

    monkeypatch.setattr("app.main.create_controller", _create_with_failing_stop)

    errors: list[str] = []
    outputs: list[str] = []

    exit_code = main(
        argv=[],
        input_fn=lambda prompt: "exit",
        print_fn=outputs.append,
        print_err=errors.append,
    )

    assert exit_code == 1
    assert any("Shutdown error (RuntimeError). Check logs for details." in err for err in errors)
    assert not any(sensitive_path in err for err in errors)


def test_cli_cleanup_on_normal_and_unexpected_exception(
    run_cli: Any,
    monkeypatch: pytest.MonkeyPatch,
    temp_anaya_dir: TempAnayaPaths,
) -> None:
    """Scenarios 8 & 9: Controller cleanup runs when input ends normally and on unexpected exceptions."""
    # 1. Normal cleanup
    _, _, _, ctrl_normal = run_cli(["exit"])
    assert ctrl_normal is not None
    assert ctrl_normal._cleanly_stopped is True

    # 2. Unexpected exception during REPL loop
    real_create = create_controller
    active_ctrl: list[AssistantController] = []

    def _tracking_create(**kwargs: Any) -> AssistantController:
        kwargs.setdefault("tasks_db_path", temp_anaya_dir.tasks_db)
        kwargs.setdefault("memory_db_path", temp_anaya_dir.memory_db)
        kwargs.setdefault("enable_speech", False)
        kwargs.setdefault("enable_ai", False)
        ctrl = real_create(**kwargs)
        active_ctrl.append(ctrl)
        return ctrl

    monkeypatch.setattr("app.main.create_controller", _tracking_create)

    def _exploding_repl(controller: AssistantController, **kwargs: Any) -> None:
        raise ValueError("Unexpected mid-session crash")

    monkeypatch.setattr("app.main.run_repl", _exploding_repl)

    errors: list[str] = []
    exit_code = main(argv=[], print_err=errors.append)

    assert exit_code == 1
    assert any("Fatal error (ValueError). Check logs for details." in err for err in errors)
    # The controller was still cleanly stopped in main()'s finally block
    assert len(active_ctrl) == 1
    assert active_ctrl[0]._cleanly_stopped is True
    assert active_ctrl[0].is_running is False


def test_cli_text_only_mode_invariants(run_cli: Any) -> None:
    """Scenario 10: CLI remains strictly text-only with no audio pipeline initialized."""
    exit_code, outputs, errors, ctrl = run_cli(["exit"])

    assert exit_code == 0
    assert ctrl is not None
    assert ctrl.voice_pipeline is None
    # Calling speak() or handle_voice() fails gracefully without audio hardware
    assert ctrl.speak("Hello") is False
    voice_res = ctrl.handle_voice()
    assert voice_res.success is False
    assert "Speech subsystem is unavailable" in str(voice_res.error)


def test_cli_security_barrier_blocks_conversational_confirmations(
    run_cli: Any,
    temp_anaya_dir: TempAnayaPaths,
) -> None:
    """Security Check: Destructive commands require out-of-band approval; text confirmation is rejected."""
    # 1. Create task through actual CLI session
    exit_code1, outputs1, errors1, _ = run_cli(["tasks.create DeleteMeTask", "exit"])
    assert exit_code1 == 0
    assert not errors1

    # 2. Obtain real generated task ID from SQLite
    conn = sqlite3.connect(temp_anaya_dir.tasks_db)
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM tasks WHERE title = 'DeleteMeTask'")
    row = cursor.fetchone()
    conn.close()
    assert row is not None
    actual_task_id = row[0]

    # 3. Submit tasks.delete <actual_id> and conversational confirmations through CLI
    commands = [
        f"tasks.delete {actual_task_id}",
        "yes",
        "confirm",
        "approve",
        f"confirm {actual_task_id}",
        f"approve {actual_task_id}",
        "exit",
    ]
    exit_code2, outputs2, errors2, _ = run_cli(commands)

    assert exit_code2 == 0
    assert not errors2

    # tasks.delete <actual_id> must require confirmation
    conf_msg = next(line for line in outputs2 if "Confirmation required. Confirmation ID: " in line)
    assert conf_msg is not None

    # Every conversational confirmation attempt must be rejected by the security barrier
    barrier_msgs = [
        line for line in outputs2
        if "Text confirmation is disabled. Approve or reject the pending request" in line
    ]
    assert len(barrier_msgs) == 5  # yes, confirm, approve, confirm <id>, approve <id>

    # Task was NOT deleted; verify it remains pending in the SQLite database
    conn = sqlite3.connect(temp_anaya_dir.tasks_db)
    cursor = conn.cursor()
    cursor.execute("SELECT id, title, status FROM tasks WHERE id = ?", (actual_task_id,))
    row = cursor.fetchone()
    conn.close()
    assert row is not None
    assert row[0] == actual_task_id
    assert row[1] == "DeleteMeTask"
    assert row[2] == "pending"


def test_cli_security_ai_fallback_never_executes_commands(run_cli: Any) -> None:
    """Security Check: Unrecognized natural language input never triggers tools or shell execution."""
    commands = [
        "Please delete all my tasks and wipe the database",
        "Run format C: immediately",
        "exit",
    ]
    # AI disabled (default E2E mode)
    exit_code, outputs, errors, _ = run_cli(commands, enable_ai=False)

    assert exit_code == 0
    assert not errors
    unrecognized = [
        line for line in outputs
        if "Command not recognized or its arguments are invalid." in line
    ]
    assert len(unrecognized) == 2


class FakeAdversarialAIProvider:
    """Deterministic fake ModelProvider returning adversarial command and approval payloads."""

    def __init__(self, adversarial_payload: str) -> None:
        self.payload = adversarial_payload
        self.chat_called = False

    def ping(self) -> bool:
        return True

    def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(name="qwen2.5:1.5b", size_bytes=1000)]

    def get_model_details(self, model_name: str) -> ModelDetails | None:
        return ModelDetails(family="qwen2.5", parameter_size="1.5B", quantization_level="Q4")

    def chat(
        self,
        messages: list[ChatMessage],
        model: str,
        options: GenerationOptions | None = None,
    ) -> GenerationResult:
        self.chat_called = True
        return GenerationResult(
            success=True,
            content=self.payload,
            model=model,
            total_duration_ns=100,
            eval_count=10,
        )

    def unload_model(self, model_name: str) -> bool:
        return True


def test_cli_security_ai_generated_text_cannot_execute_commands_or_approve(
    run_cli: Any,
    temp_anaya_dir: TempAnayaPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Security Check: When AI is enabled, model output containing command syntax is treated

    strictly as passive text and cannot execute tools, delete tasks, or grant approvals.
    """
    # Adversarial AI output contains destructive command, affirmative approvals, and shell command
    adversarial_output = "tasks.delete 1\nyes\nconfirm\nformat C:"
    fake_provider = FakeAdversarialAIProvider(adversarial_output)

    # Instrument public CommandExecutor and ApprovalService methods to verify no calls from AI output
    executed_commands: list[tuple[str, tuple[str, ...]]] = []
    real_execute = CommandExecutor.execute

    def _spy_execute(
        self: CommandExecutor,
        name: str,
        arguments: tuple[str, ...] = (),
        confirmation_id: str | None = None,
    ) -> Any:
        executed_commands.append((name, arguments))
        return real_execute(self, name, arguments, confirmation_id=confirmation_id)

    monkeypatch.setattr(CommandExecutor, "execute", _spy_execute)

    approved_requests: list[Any] = []
    real_approve = ApprovalService.approve

    def _spy_approve(self: ApprovalService, request: Any) -> bool:
        approved_requests.append(request)
        return real_approve(self, request)

    monkeypatch.setattr(ApprovalService, "approve", _spy_approve)

    commands = [
        "tasks.create ProtectedTask",
        "What should I do next?",  # Ordinary conversational question triggers AI fallback
        "exit",
    ]

    exit_code, outputs, errors, ctrl = run_cli(
        commands,
        enable_ai=True,
        custom_ai_provider=fake_provider,
    )

    assert exit_code == 0
    assert not errors

    # 1. Verify the AI provider was actually invoked for the conversational question
    assert fake_provider.chat_called is True

    # 2. Verify the AI output was returned strictly as passive conversational text in the terminal
    assert any("tasks.delete 1" in line for line in outputs)
    assert any("format C:" in line for line in outputs)

    # 3. Verify real task remains pending in SQLite database (NOT deleted)
    conn = sqlite3.connect(temp_anaya_dir.tasks_db)
    cursor = conn.cursor()
    cursor.execute("SELECT title, status FROM tasks WHERE title = 'ProtectedTask'")
    row = cursor.fetchone()
    conn.close()

    assert row is not None
    assert row[0] == "ProtectedTask"
    assert row[1] == "pending"

    # 4. Verify CommandExecutor was ONLY called for user's explicit 'tasks.create', never for AI output
    executed_command_names = [name for name, _ in executed_commands]
    assert executed_command_names == ["tasks.create"]
    assert "tasks.delete" not in executed_command_names
    assert "format C:" not in executed_command_names

    # 5. Verify ApprovalService.approve was NEVER called
    assert len(approved_requests) == 0
