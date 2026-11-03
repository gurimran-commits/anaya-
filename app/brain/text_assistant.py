import re
import shlex
from typing import TYPE_CHECKING

from app.brain.context import ConversationContext
from app.brain.intent_handler import IntentHandler
from app.brain.models import IntentStatus
from app.commands.executor import CommandExecutor
from app.commands.registry import CommandRegistry
from app.core.action_result import ActionResult

if TYPE_CHECKING:
    from app.ai.manager import ModelManager
    from app.memory.memory_manager import MemoryManager
    from app.skills.skill_manager import SkillManager
    from app.tasks.task_manager import TaskManager


class TextAssistant:
    """Process natural language and text commands without bypassing security controls."""

    def __init__(
        self,
        registry: CommandRegistry,
        executor: CommandExecutor,
        memory_manager: "MemoryManager | None" = None,
        skill_manager: "SkillManager | None" = None,
        task_manager: "TaskManager | None" = None,
        model_manager: "ModelManager | None" = None,
    ) -> None:
        self._registry = registry
        self._executor = executor
        self._memory_manager = memory_manager
        self._skill_manager = skill_manager
        self._task_manager = task_manager
        self._model_manager = model_manager
        if memory_manager is not None:
            from app.commands.memory_commands import register_memory_commands

            register_memory_commands(registry, memory_manager)
        if skill_manager is not None:
            from app.commands.skill_commands import register_skill_commands

            register_skill_commands(registry, skill_manager, executor)
        if task_manager is not None:
            from app.commands.task_commands import register_task_commands

            register_task_commands(registry, task_manager)
        self._intent_handler = IntentHandler(registry)
        self._context = ConversationContext()

    @property
    def memory_manager(self) -> "MemoryManager | None":
        """Expose optional MemoryManager for memory operations."""
        return self._memory_manager

    @property
    def skill_manager(self) -> "SkillManager | None":
        """Expose optional SkillManager for skill operations."""
        return self._skill_manager

    @property
    def task_manager(self) -> "TaskManager | None":
        """Expose optional TaskManager for task operations."""
        return self._task_manager

    @property
    def model_manager(self) -> "ModelManager | None":
        """Expose optional ModelManager for conversational AI operations."""
        return self._model_manager

    @property
    def context(self) -> ConversationContext:
        """Expose current conversation context for inspection and testing."""
        return self._context

    def reset_context(self) -> None:
        """Reset conversational context and clear any pending clarification."""
        self._context.reset()
        if self._model_manager is not None:
            self._model_manager.reset_history()

    @staticmethod
    def _is_command_like(text: str) -> bool:
        """Detect whether input appears to be an explicit command, CLI call, or executable syntax."""
        stripped = text.strip()
        if not stripped:
            return False

        first_word = stripped.split()[0]
        clean_first_word = first_word.rstrip("?!,;:")

        if clean_first_word.lower() in {"e.g.", "i.e.", "e.g", "i.e", "dr.", "mr.", "ms.", "mrs.", "vs.", "vs"}:
            return False

        # Dotted command names: e.g. files.read, system.reboot, custom.action
        if "." in clean_first_word and not clean_first_word.startswith(".") and not clean_first_word.endswith("."):
            if re.match(r"^[a-zA-Z_][a-zA-Z0-9_-]*(?:\.[a-zA-Z_][a-zA-Z0-9_-]*)+$", clean_first_word):
                return True

        # Path executions: ./script, .\script, /bin/sh, C:\foo
        if stripped.startswith(("./", ".\\", "../", "..\\", "/", "\\")) or bool(re.match(r"^[a-zA-Z]:[\\/]", stripped)):
            return True

        # Common shell / CLI command prefixes
        cli_commands = {
            "sudo", "bash", "sh", "zsh", "cmd", "cmd.exe", "powershell", "powershell.exe",
            "pwsh", "rm", "rmdir", "del", "erase", "mkdir", "kill", "pkill", "killall",
            "chmod", "chown", "curl", "wget", "git", "python", "python3", "pip", "npm",
            "node", "cat", "ls", "dir", "grep", "mv", "cp", "copy", "move", "format",
            "reboot", "shutdown", "taskkill", "attrib", "icacls", "reg", "sc", "net",
        }
        if clean_first_word.lower() in cli_commands:
            return True

        # Shell operators: piping, redirection, chaining
        if any(op in text for op in ("|", "&&", "||", ";", ">>", ">", "<")):
            return True

        # Malformed quotes or escaping in command syntax
        try:
            shlex.split(stripped)
        except ValueError:
            return True

        return False

    def process(self, text: str) -> ActionResult:
        """Process user text input, parse intent, and execute registered operations."""
        if not isinstance(text, str):
            return ActionResult(False, "Please enter a text command.")

        text = text.strip()
        if not text:
            return ActionResult(False, "Please enter a command.")

        casefolded = text.casefold()

        if casefolded in {"help", "commands"}:
            names = sorted(
                command.name for command in self._registry.list_commands()
            )
            available = (
                ", ".join(names) if names else "No commands registered."
            )
            return ActionResult(
                True,
                f"Available commands: {available}. "
                "Use the exact command name followed by its arguments.",
            )

        # Enforce security barrier: text confirmation is strictly forbidden.
        if (
            casefolded.startswith("confirm ")
            or casefolded.startswith("approve ")
            or casefolded in {"confirm", "approve", "yes"}
        ):
            return ActionResult(
                False,
                "Text confirmation is disabled. Approve or reject the "
                "pending request through the application's explicit "
                "approval interface.",
            )

        # Handle direct context reset command.
        if casefolded in {"reset", "reset context", "clear context", "clear history"}:
            self.reset_context()
            return ActionResult(True, "Context has been reset.")

        # Handle direct cancellation.
        if casefolded in {"cancel", "nevermind", "abort", "stop"}:
            if self._context.has_pending:
                self._context.clear_pending()
                return ActionResult(False, "Action cancelled.")
            return ActionResult(False, "Nothing to cancel.")

        resolution = self._intent_handler.resolve(text, self._context)

        if resolution.status == IntentStatus.CANCELLED:
            self._context.clear_pending()
            return ActionResult(False, resolution.message or "Action cancelled.")

        if resolution.status == IntentStatus.RESET_CONTEXT:
            self.reset_context()
            return ActionResult(True, resolution.message or "Context has been reset.")

        if resolution.status in {
            IntentStatus.MISSING_ARGUMENT,
            IntentStatus.AMBIGUOUS,
            IntentStatus.UNSUPPORTED,
        }:
            if resolution.status == IntentStatus.UNSUPPORTED:
                # Distinguish explicit unsupported operation categories from generic unrecognized inputs.
                is_explicit_unsupported = bool(
                    resolution.message
                    and resolution.message != (
                        "Command not recognized or its arguments are invalid. "
                        "Type 'help' to see registered commands."
                    )
                )
                can_fallback_to_ai = (
                    not is_explicit_unsupported
                    and not self._context.has_pending
                    and not self._is_command_like(text)
                    and self._model_manager is not None
                    and self._model_manager.is_enabled
                )
                if can_fallback_to_ai:
                    ai_response = self._model_manager.generate_response(text)
                    if ai_response is not None:
                        return ActionResult(True, ai_response)

            return ActionResult(
                False,
                resolution.message
                or (
                    "Command not recognized or its arguments are invalid. "
                    "Type 'help' to see registered commands."
                ),
            )

        if resolution.status == IntentStatus.INVALID:
            msg = resolution.message or (
                "Command not recognized or its arguments are invalid. "
                "Type 'help' to see registered commands."
            )
            if "arguments are invalid" in msg and "help" not in msg:
                msg = f"{msg} Type 'help' to see registered commands."
            return ActionResult(False, msg)

        if resolution.status == IntentStatus.VALID and resolution.intent is not None:
            intent = resolution.intent
            result = self._executor.execute(
                intent.name,
                intent.arguments,
            )

            if result.message.startswith("Confirmation required. Confirmation ID: "):
                # Record target in context so user can reference it in follow-ups.
                if resolution.target:
                    self._context.update(
                        intent.name,
                        target=resolution.target,
                        directory_index=resolution.directory_index,
                    )
                return ActionResult(
                    False,
                    result.message
                    + " Approve or reject this request through the "
                    "application's explicit approval interface.",
                )

            if result.success:
                self._context.update(
                    intent.name,
                    target=resolution.target,
                    directory_index=resolution.directory_index,
                )

            return result

        if (
            not self._context.has_pending
            and not self._is_command_like(text)
            and self._model_manager is not None
            and self._model_manager.is_enabled
        ):
            ai_response = self._model_manager.generate_response(text)
            if ai_response is not None:
                return ActionResult(True, ai_response)

        return ActionResult(
            False,
            "Command not recognized or its arguments are invalid. "
            "Type 'help' to see registered commands.",
        )
