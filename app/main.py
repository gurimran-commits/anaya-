"""Minimal text-based command-line interface entry point for Anaya."""

from __future__ import annotations

import argparse
import logging
import sys
from typing import TYPE_CHECKING, Any, Callable

from app.bootstrap import create_controller

if TYPE_CHECKING:
    from app.controller import AssistantController

logger = logging.getLogger(__name__)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for Anaya CLI."""
    parser = argparse.ArgumentParser(
        prog="anaya",
        description="Anaya AI Desktop Assistant",
    )
    parser.add_argument(
        "--desktop",
        action="store_true",
        help="Run Anaya in desktop mode with system tray, hotkeys, and HUD.",
    )
    return parser.parse_args(argv)


def run_repl(
    controller: AssistantController,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[..., None] = print,
) -> None:
    """Run the interactive text REPL loop.

    Handles empty input, exit commands, EOF, and keyboard interrupts cleanly.
    """
    print_fn("Anaya Assistant (Text Mode)")
    print_fn("Type 'help' for available commands, or 'exit' / 'quit' to close.\n")

    while True:
        try:
            raw = input_fn("anaya> ")
        except (EOFError, KeyboardInterrupt):
            print_fn("\nGoodbye!")
            break

        cleaned = raw.strip()
        if not cleaned:
            continue

        if cleaned.casefold() in {"exit", "quit", ":q"}:
            print_fn("Goodbye!")
            break

        try:
            result = controller.handle_text(cleaned)
            if result.message:
                print_fn(result.message)
        except KeyboardInterrupt:
            print_fn("\nCommand interrupted.")


def main(
    argv: list[str] | None = None,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[..., None] = print,
    print_err: Callable[..., None] | None = None,
) -> int:
    """Application entry point returning shell exit code (0 for success, non-zero for error).

    Guarantees:
        - Import-safe: only runs when explicitly called.
        - Text-only: speech subsystem disabled by default.
        - Deterministic cleanup: controller.stop() is always attempted in finally.
        - Sanitized error output without leaking secrets or credentials.
    """
    if print_err is None:
        def _default_err(*args: Any, **kwargs: Any) -> None:
            print(*args, file=sys.stderr, **kwargs)
        print_err = _default_err

    try:
        args = parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)

    if args.desktop:
        try:
            from app.core.config import AppConfig
            from app.desktop.runner import DesktopApplicationRunner

            config = AppConfig(desktop_enabled=True, hotkey_enabled=True)
            runner = DesktopApplicationRunner(config=config)
            return runner.run()
        except Exception as exc:
            logger.error("Desktop application fatal error (%s): %s", type(exc).__name__, exc, exc_info=True)
            print_err(f"Desktop fatal error ({type(exc).__name__}). Check logs for details.")
            return 1

    controller: AssistantController | None = None
    exit_code = 0

    try:
        controller = create_controller(enable_speech=False)
        controller.start()
        run_repl(controller, input_fn=input_fn, print_fn=print_fn)
    except Exception as exc:
        logger.error("Application fatal error (%s): %s", type(exc).__name__, exc, exc_info=True)
        print_err(f"Fatal error ({type(exc).__name__}). Check logs for details.")
        exit_code = 1
    finally:
        if controller is not None:
            try:
                controller.stop()
            except Exception as exc:
                logger.error(
                    "Error during application shutdown (%s): %s",
                    type(exc).__name__,
                    exc,
                    exc_info=True,
                )
                print_err(
                    f"Shutdown error ({type(exc).__name__}). Check logs for details."
                )
                exit_code = 1

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
