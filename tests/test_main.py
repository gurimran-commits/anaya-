"""Tests for Anaya CLI application entry point and REPL loop."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.core.action_result import ActionResult
from app.main import main, parse_args, run_repl


def test_main_import_safety():
    """Importing app.main must not start the controller, construct databases, or prompt for input."""
    import app.main

    assert callable(app.main.main)
    assert callable(app.main.run_repl)
    assert callable(app.main.parse_args)


def test_main_clean_exit_commands():
    """Typing 'exit', 'quit', or ':q' terminates the REPL loop cleanly with exit code 0."""
    for cmd in ["exit", "quit", ":q", "  EXIT  "]:
        mock_controller = MagicMock()
        mock_controller.start = MagicMock()
        mock_controller.stop = MagicMock()

        outputs: list[str] = []

        with patch("app.main.create_controller", return_value=mock_controller):
            exit_code = main(
                argv=[],
                input_fn=lambda prompt: cmd,
                print_fn=outputs.append,
            )

        assert exit_code == 0
        assert mock_controller.start.called
        assert mock_controller.stop.called
        assert any("Goodbye!" in out for out in outputs)
        assert not mock_controller.handle_text.called


def test_main_eof_handling():
    """EOFError during input prints an exit message and terminates the loop with exit code 0."""
    mock_controller = MagicMock()
    outputs: list[str] = []

    def eof_input(prompt: str):
        raise EOFError()

    with patch("app.main.create_controller", return_value=mock_controller):
        exit_code = main(
            argv=[],
            input_fn=eof_input,
            print_fn=outputs.append,
        )

    assert exit_code == 0
    assert mock_controller.stop.called
    assert any("Goodbye!" in out for out in outputs)


def test_main_keyboard_interrupt_during_input():
    """KeyboardInterrupt at the input prompt terminates cleanly with exit code 0."""
    mock_controller = MagicMock()
    outputs: list[str] = []

    def sigint_input(prompt: str):
        raise KeyboardInterrupt()

    with patch("app.main.create_controller", return_value=mock_controller):
        exit_code = main(
            argv=[],
            input_fn=sigint_input,
            print_fn=outputs.append,
        )

    assert exit_code == 0
    assert mock_controller.stop.called
    assert any("Goodbye!" in out for out in outputs)


def test_main_keyboard_interrupt_during_handle_text():
    """KeyboardInterrupt during command execution catches interrupt and stays in loop."""
    mock_controller = MagicMock()
    mock_controller.handle_text.side_effect = KeyboardInterrupt()

    inputs = iter(["long running task", "exit"])
    outputs: list[str] = []

    with patch("app.main.create_controller", return_value=mock_controller):
        exit_code = main(
            argv=[],
            input_fn=lambda prompt: next(inputs),
            print_fn=outputs.append,
        )

    assert exit_code == 0
    assert mock_controller.handle_text.called
    assert any("Command interrupted." in out for out in outputs)
    assert any("Goodbye!" in out for out in outputs)


def test_main_text_forwarding_and_display():
    """User input is passed to controller.handle_text() and ActionResult.message is printed."""
    mock_controller = MagicMock()
    mock_controller.handle_text.return_value = ActionResult(
        success=True,
        message="Created task: 'test item'",
    )

    inputs = iter(["add task test item", "quit"])
    outputs: list[str] = []

    with patch("app.main.create_controller", return_value=mock_controller):
        exit_code = main(
            argv=[],
            input_fn=lambda prompt: next(inputs),
            print_fn=outputs.append,
        )

    assert exit_code == 0
    mock_controller.handle_text.assert_called_once_with("add task test item")
    assert "Created task: 'test item'" in outputs


def test_main_empty_input_skipped():
    """Empty or whitespace-only lines do not invoke controller.handle_text()."""
    mock_controller = MagicMock()
    inputs = iter(["", "   ", "\t", "exit"])
    outputs: list[str] = []

    with patch("app.main.create_controller", return_value=mock_controller):
        exit_code = main(
            argv=[],
            input_fn=lambda prompt: next(inputs),
            print_fn=outputs.append,
        )

    assert exit_code == 0
    assert not mock_controller.handle_text.called


def test_main_startup_failure_handling():
    """When controller creation fails, main() reports sanitized error and returns 1."""
    errors: list[str] = []

    with patch("app.main.create_controller", side_effect=RuntimeError("SensitiveDBPath /secret/key failed")):
        exit_code = main(
            argv=[],
            print_err=errors.append,
        )

    assert exit_code == 1
    # Verify sanitized output does not leak raw exception message
    assert any("Fatal error (RuntimeError). Check logs for details." in err for err in errors)
    assert not any("SensitiveDBPath" in err for err in errors)
    assert not any("/secret/key" in err for err in errors)


def test_main_controller_start_failure_attempts_stop():
    """When controller.start() fails, controller.stop() is still attempted and exit code is 1."""
    mock_controller = MagicMock()
    mock_controller.start.side_effect = RuntimeError("Start worker thread failure")
    errors: list[str] = []

    with patch("app.main.create_controller", return_value=mock_controller):
        exit_code = main(
            argv=[],
            print_err=errors.append,
        )

    assert exit_code == 1
    assert mock_controller.start.called
    assert mock_controller.stop.called
    assert any("Fatal error (RuntimeError). Check logs for details." in err for err in errors)
    assert not any("Start worker thread failure" in err for err in errors)


def test_main_startup_and_shutdown_both_fail_preserves_startup_error(caplog):
    """When both start() and stop() raise, main() returns 1 and startup error is preserved in logs."""
    import logging

    mock_controller = MagicMock()
    mock_controller.start.side_effect = RuntimeError("Fatal startup error: sensitive connection string")
    mock_controller.stop.side_effect = RuntimeError("Fatal shutdown timeout")
    errors: list[str] = []

    with caplog.at_level(logging.ERROR):
        with patch("app.main.create_controller", return_value=mock_controller):
            exit_code = main(
                argv=[],
                print_err=errors.append,
            )

    assert exit_code == 1
    # Terminal output is sanitized
    assert any("Fatal error (RuntimeError). Check logs for details." in err for err in errors)
    assert not any("sensitive connection string" in err for err in errors)

    # Diagnostic logs record BOTH the original startup failure and the shutdown failure
    log_text = caplog.text
    assert "Application fatal error (RuntimeError)" in log_text
    assert "sensitive connection string" in log_text
    assert "Error during application shutdown (RuntimeError)" in log_text
    assert "Fatal shutdown timeout" in log_text


def test_main_shutdown_always_called_on_repl_exception():
    """Controller.stop() is always invoked in finally even if an unhandled error occurs in REPL."""
    mock_controller = MagicMock()

    def crashing_input(prompt: str):
        raise ValueError("Unexpected terminal failure")

    with patch("app.main.create_controller", return_value=mock_controller):
        exit_code = main(
            argv=[],
            input_fn=crashing_input,
            print_err=lambda msg: None,
        )

    assert exit_code == 1
    assert mock_controller.stop.called


def test_main_shutdown_failure_returns_error_code():
    """If controller.stop() raises an exception during shutdown, main() prints sanitized error and returns 1."""
    mock_controller = MagicMock()
    mock_controller.stop.side_effect = RuntimeError("Sensitive shutdown details")
    errors: list[str] = []

    with patch("app.main.create_controller", return_value=mock_controller):
        exit_code = main(
            argv=[],
            input_fn=lambda prompt: "exit",
            print_fn=lambda msg: None,
            print_err=errors.append,
        )

    assert exit_code == 1
    assert any("Shutdown error (RuntimeError). Check logs for details." in err for err in errors)
    assert not any("Sensitive shutdown details" in err for err in errors)


def test_main_text_only_mode_verified():
    """Verify that create_controller is called with enable_speech=False."""
    with patch("app.main.create_controller") as mock_create:
        mock_ctrl = MagicMock()
        mock_create.return_value = mock_ctrl

        main(
            argv=[],
            input_fn=lambda prompt: "exit",
            print_fn=lambda msg: None,
        )

        mock_create.assert_called_once_with(enable_speech=False)


def test_main_help_system_exit():
    """main(['--help']) handles argparse SystemExit and returns 0."""
    exit_code = main(argv=["--help"])
    assert exit_code == 0


def test_main_invalid_arg_system_exit():
    """main(['--invalid-flag']) handles argparse SystemExit and returns non-zero."""
    exit_code = main(argv=["--invalid-flag-xyz"])
    assert exit_code == 2
