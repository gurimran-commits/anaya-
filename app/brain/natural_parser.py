import re
import shlex
from typing import TYPE_CHECKING

from app.brain.models import CommandIntent, IntentResolution, IntentStatus

if TYPE_CHECKING:
    from app.brain.context import ConversationContext
    from app.commands.registry import CommandRegistry


class NaturalLanguageParser:
    """Interpret natural language commands and map to registered operations.

    Operates deterministically using rule-based parsing with zero external dependencies.
    """

    # Bounded input length.
    MAX_INPUT_LENGTH = 4096

    # Context management commands.
    _RESET_PATTERNS = {
        "reset",
        "reset context",
        "clear context",
        "reset conversation",
        "clear history",
        "forget context",
    }

    _CANCEL_PATTERNS = {
        "cancel",
        "nevermind",
        "abort",
        "stop",
    }

    # Unsupported intent detectors.
    _UNSUPPORTED_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
        (
            re.compile(
                r"^(?:please\s+)?(?:create|make|mkdir|remove|delete|rmdir)\s+(?:a\s+)?(?:new\s+)?(?:folder|directory)\b",
                re.IGNORECASE,
            ),
            (
                "Folder operations are not currently supported by registered commands. "
                "Available commands: files.list, files.read, files.create, files.delete. "
                "Type 'help' to see registered commands."
            ),
        ),
        (
            re.compile(
                r"^(?:please\s+)?(?:send|write)\s+(?:an?\s+)?(?:email|message)\b",
                re.IGNORECASE,
            ),
            (
                "Messaging and email operations are not supported by registered commands. "
                "Type 'help' to see available commands."
            ),
        ),
        (
            re.compile(
                r"^(?:please\s+)?(?:search|browse|google|look\s+up)\b",
                re.IGNORECASE,
            ),
            (
                "Web browsing and search operations are not supported. "
                "Type 'help' to see available commands."
            ),
        ),
        (
            re.compile(
                r"^(?:please\s+)?(?:play|stream)\s+(?:music|song|audio|video)\b",
                re.IGNORECASE,
            ),
            (
                "Media playback operations are not supported by registered commands. "
                "Type 'help' to see available commands."
            ),
        ),
        (
            re.compile(
                r"^(?:please\s+)?(?:shutdown|reboot|restart\s+computer|kill\s+process)\b",
                re.IGNORECASE,
            ),
            (
                "System power and process operations are not supported by registered commands."
            ),
        ),
    )

    # Ambiguous commands when without target or context.
    _AMBIGUOUS_PATTERNS: tuple[re.Pattern, ...] = (
        re.compile(
            r"^(?:please\s+)?(?:open|view|inspect|process|run|execute|do|check)\s+(?:it|that|this)$",
            re.IGNORECASE,
        ),
        re.compile(
            r"^(?:please\s+)?(?:open|run|execute|process|do\s+something|check)$",
            re.IGNORECASE,
        ),
        re.compile(
            r"^(?:please\s+)?(?:create|make)\s+(?:it|that)$",
            re.IGNORECASE,
        ),
    )

    @staticmethod
    def clean_quotes(text: str) -> str:
        """Strip surrounding matching quotes and whitespace."""
        text = text.strip()
        if (text.startswith('"') and text.endswith('"')) or (
            text.startswith("'") and text.endswith("'")
        ):
            if len(text) >= 2:
                text = text[1:-1].strip()
        return text

    @staticmethod
    def _quoted_spans(text: str) -> list[tuple[int, int]]:
        """Return [start, end) index spans of quoted substrings."""
        spans = []
        in_quote = None
        start = -1
        for i, char in enumerate(text):
            if char in ('"', "'"):
                if in_quote is None:
                    in_quote = char
                    start = i
                elif in_quote == char:
                    spans.append((start, i + 1))
                    in_quote = None
                    start = -1
        if in_quote is not None and start != -1:
            spans.append((start, len(text)))
        return spans

    @classmethod
    def _extract_directory(cls, text: str) -> tuple[str, str]:
        """Extract explicit directory index if specified outside quoted spans, e.g. 'in folder 1'."""
        pattern = re.compile(
            r"\b(?:in|from|within)\s+(?:the\s+)?(?:folder|directory|dir)\s+(?:index\s+|number\s+)?(?P<dir>\d+)\b",
            re.IGNORECASE,
        )
        quoted_spans = cls._quoted_spans(text)

        for match in pattern.finditer(text):
            match_start = match.start()
            is_inside_quote = any(
                q_start <= match_start < q_end for q_start, q_end in quoted_spans
            )
            if not is_inside_quote:
                directory_index = match.group("dir")
                cleaned_text = (
                    text[: match.start()] + text[match.end() :]
                ).strip()
                cleaned_text = re.sub(r"\s+", " ", cleaned_text)
                return directory_index, cleaned_text

        return "0", text

    def _detect_new_command_intent(
        self,
        text: str,
        directory_index: str,
        context: "ConversationContext",
        registry: "CommandRegistry",
    ) -> IntentResolution | None:
        """Check whether input clearly represents a new command rather than an argument."""
        # 1. Exact command registration check (via shlex split).
        try:
            parts = shlex.split(text)
            if parts and registry.get(parts[0]) is not None:
                tuple_args = tuple(parts[1:])
                if registry.validate(parts[0], tuple_args):
                    target = tuple_args[1] if len(tuple_args) >= 2 else (tuple_args[0] if tuple_args else None)
                    return IntentResolution(
                        status=IntentStatus.VALID,
                        intent=CommandIntent(parts[0], tuple_args),
                        target=target,
                        directory_index=directory_index,
                    )
        except ValueError:
            pass

        # 2. File listing intent.
        if registry.get("files.list"):
            list_res = self._match_list(text, directory_index)
            if list_res is not None:
                return list_res

        # 3. File creation intent.
        if registry.get("files.create"):
            create_res = self._match_create(text, directory_index, context)
            if create_res is not None:
                return create_res

        # 4. File deletion intent.
        if registry.get("files.delete"):
            delete_res = self._match_delete(text, directory_index, context)
            if delete_res is not None:
                return delete_res

        # 5. File read intent with explicit command phrasing.
        if registry.get("files.read"):
            read_res = self._match_read(text, directory_index, context)
            if read_res is not None and read_res.status == IntentStatus.VALID:
                return read_res

        return None

    def parse(
        self,
        text: str,
        context: "ConversationContext",
        registry: "CommandRegistry",
    ) -> IntentResolution:
        """Parse natural language into a structured IntentResolution."""
        text = text.strip()
        if not text:
            return IntentResolution(
                status=IntentStatus.INVALID,
                message="Please enter a command.",
            )

        if len(text) > self.MAX_INPUT_LENGTH:
            return IntentResolution(
                status=IntentStatus.INVALID,
                message=(
                    f"Input exceeds maximum permitted length of {self.MAX_INPUT_LENGTH} characters."
                ),
            )

        casefolded = text.casefold()

        # Check for context reset request.
        if casefolded in self._RESET_PATTERNS:
            return IntentResolution(
                status=IntentStatus.RESET_CONTEXT,
                message="Context has been reset.",
            )

        # Check for pending clarification cancellation.
        if context.has_pending and casefolded in self._CANCEL_PATTERNS:
            return IntentResolution(
                status=IntentStatus.CANCELLED,
                message="Action cancelled.",
            )

        # Check for explicit unsupported operations.
        for pattern, message in self._UNSUPPORTED_PATTERNS:
            if pattern.search(text):
                if context.has_pending:
                    context.clear_pending()
                return IntentResolution(
                    status=IntentStatus.UNSUPPORTED,
                    message=message,
                )

        # Extract directory specification outside quoted filename.
        dir_idx, cleaned_text = self._extract_directory(text)

        # Handle pending clarification with precedence checking.
        if context.has_pending:
            # Check if user entered a completely new command intent instead of answering.
            new_intent = self._detect_new_command_intent(
                cleaned_text, dir_idx, context, registry
            )
            if new_intent is not None:
                context.clear_pending()
                return new_intent

            # Otherwise, resolve as answer to pending clarification.
            clarification_resolution = self._resolve_pending_clarification(
                text, context, registry
            )
            if clarification_resolution is not None:
                return clarification_resolution

        # Check for ambiguous requests without context.
        for pattern in self._AMBIGUOUS_PATTERNS:
            if pattern.match(cleaned_text):
                return IntentResolution(
                    status=IntentStatus.AMBIGUOUS,
                    message=(
                        "Your request is ambiguous because no previous file or target was referenced. "
                        "Please specify the action and target."
                    ),
                )

        # 1. Match File Listing (files.list).
        if registry.get("files.list"):
            list_res = self._match_list(cleaned_text, dir_idx)
            if list_res is not None:
                return list_res

        # 2. Match File Reading (files.read).
        if registry.get("files.read"):
            read_res = self._match_read(cleaned_text, dir_idx, context)
            if read_res is not None:
                return read_res

        # 3. Match File Creation (files.create).
        if registry.get("files.create"):
            create_res = self._match_create(cleaned_text, dir_idx, context)
            if create_res is not None:
                return create_res

        # 4. Match File Deletion (files.delete).
        if registry.get("files.delete"):
            delete_res = self._match_delete(cleaned_text, dir_idx, context)
            if delete_res is not None:
                return delete_res

        # If no natural language pattern matched, return unsupported/unrecognized.
        return IntentResolution(
            status=IntentStatus.UNSUPPORTED,
            message=(
                "Command not recognized or its arguments are invalid. "
                "Type 'help' to see registered commands."
            ),
        )

    def _resolve_pending_clarification(
        self,
        text: str,
        context: "ConversationContext",
        registry: "CommandRegistry",
    ) -> IntentResolution | None:
        """Handle user input that provides the answer to a pending clarification."""
        pending_intent = context.pending_intent
        if not pending_intent:
            return None

        # Clean the input argument.
        argument = self.clean_quotes(text)
        if not argument:
            return None

        dir_idx = context.pending_directory_index

        if pending_intent == "files.read":
            intent = CommandIntent(pending_intent, (dir_idx, argument))
            if registry.validate(intent.name, intent.arguments):
                return IntentResolution(
                    status=IntentStatus.VALID,
                    intent=intent,
                    target=argument,
                    directory_index=dir_idx,
                )
            return IntentResolution(
                status=IntentStatus.INVALID,
                message=f"Filename '{argument}' is invalid.",
            )

        if pending_intent == "files.delete":
            intent = CommandIntent(pending_intent, (dir_idx, argument))
            if registry.validate(intent.name, intent.arguments):
                return IntentResolution(
                    status=IntentStatus.VALID,
                    intent=intent,
                    target=argument,
                    directory_index=dir_idx,
                )
            return IntentResolution(
                status=IntentStatus.INVALID,
                message=f"Filename '{argument}' is invalid.",
            )

        if pending_intent == "files.create":
            target = context.last_target or argument
            content = argument if context.last_target else ""
            intent = CommandIntent(pending_intent, (dir_idx, target, content))
            if registry.validate(intent.name, intent.arguments):
                return IntentResolution(
                    status=IntentStatus.VALID,
                    intent=intent,
                    target=target,
                    directory_index=dir_idx,
                )
            return IntentResolution(
                status=IntentStatus.INVALID,
                message="Arguments provided for file creation are invalid.",
            )

        return None

    def _match_list(
        self,
        text: str,
        directory_index: str,
    ) -> IntentResolution | None:
        """Match natural language patterns for listing files."""
        patterns = (
            re.compile(
                r"^(?:please\s+)?(?:list|show|view|display)\s+(?:all\s+|my\s+|the\s+)?files?$",
                re.IGNORECASE,
            ),
            re.compile(
                r"^what\s+files\s+(?:are\s+here|are\s+in\s+this\s+directory|are\s+in\s+this\s+folder|do\s+i\s+have)\??$",
                re.IGNORECASE,
            ),
            re.compile(r"^(?:dir|ls)$", re.IGNORECASE),
        )

        for pattern in patterns:
            if pattern.match(text):
                return IntentResolution(
                    status=IntentStatus.VALID,
                    intent=CommandIntent("files.list", (directory_index,)),
                    directory_index=directory_index,
                )
        return None

    def _match_read(
        self,
        text: str,
        directory_index: str,
        context: "ConversationContext",
    ) -> IntentResolution | None:
        """Match natural language patterns for reading files."""
        # Check pronoun / follow-up: "read it", "read that file", "show its contents".
        pronoun_pattern = re.compile(
            r"^(?:please\s+)?(?:read|open|view|display|cat)\s+(?:it|that|that\s+file|this\s+file)$",
            re.IGNORECASE,
        )
        if pronoun_pattern.match(text):
            if context.has_target:
                return IntentResolution(
                    status=IntentStatus.VALID,
                    intent=CommandIntent(
                        "files.read",
                        (context.last_directory_index, context.last_target),  # type: ignore
                    ),
                    target=context.last_target,
                    directory_index=context.last_directory_index,
                )
            return IntentResolution(
                status=IntentStatus.AMBIGUOUS,
                message=(
                    "Your request is ambiguous because no previous file was referenced. "
                    "Please specify the filename."
                ),
            )

        # Check missing argument: "read file", "read a file", "show file contents".
        missing_arg_patterns = (
            re.compile(r"^(?:please\s+)?read\s+(?:the\s+|a\s+)?file$", re.IGNORECASE),
            re.compile(r"^(?:please\s+)?show\s+file\s+contents?$", re.IGNORECASE),
            re.compile(r"^(?:please\s+)?open\s+file$", re.IGNORECASE),
        )
        for pattern in missing_arg_patterns:
            if pattern.match(text):
                context.set_pending(
                    "files.read",
                    "Which file would you like to read? Please specify the filename.",
                    directory_index,
                )
                return IntentResolution(
                    status=IntentStatus.MISSING_ARGUMENT,
                    missing_argument_name="filename",
                    message="Which file would you like to read? Please specify the filename.",
                    directory_index=directory_index,
                )

        # Check explicit file reading.
        patterns = (
            re.compile(
                r"^(?:please\s+)?(?:read|view|cat|display|open\s+and\s+read)\s+(?:the\s+)?(?:file\s+)?(?P<name>.+)$",
                re.IGNORECASE,
            ),
            re.compile(
                r"^(?:please\s+)?show\s+(?:the\s+)?contents\s+of\s+(?:file\s+)?(?P<name>.+)$",
                re.IGNORECASE,
            ),
        )
        for pattern in patterns:
            match = pattern.match(text)
            if match:
                raw_name = match.group("name").strip()
                name = self.clean_quotes(raw_name)
                if not name or name.casefold() in {"file", "the file"}:
                    context.set_pending(
                        "files.read",
                        "Which file would you like to read? Please specify the filename.",
                        directory_index,
                    )
                    return IntentResolution(
                        status=IntentStatus.MISSING_ARGUMENT,
                        missing_argument_name="filename",
                        message="Which file would you like to read? Please specify the filename.",
                        directory_index=directory_index,
                    )

                return IntentResolution(
                    status=IntentStatus.VALID,
                    intent=CommandIntent("files.read", (directory_index, name)),
                    target=name,
                    directory_index=directory_index,
                )

        return None

    def _match_create(
        self,
        text: str,
        directory_index: str,
        context: "ConversationContext",
    ) -> IntentResolution | None:
        """Match natural language patterns for creating files."""
        # 1. Missing argument: "create file", "make a file".
        missing_arg_pattern = re.compile(
            r"^(?:please\s+)?(?:create|make)\s+(?:a\s+)?(?:new\s+)?file$",
            re.IGNORECASE,
        )
        if missing_arg_pattern.match(text):
            context.set_pending(
                "files.create",
                "Please specify the filename to create.",
                directory_index,
            )
            return IntentResolution(
                status=IntentStatus.MISSING_ARGUMENT,
                missing_argument_name="filename",
                message="Please specify the filename to create.",
                directory_index=directory_index,
            )

        # 2. Follow-up creation: "now create another [file] [called|named] <name> with [content] <content>".
        followup_pattern = re.compile(
            r"^(?:please\s+)?(?:now\s+)?(?:create|make)\s+another\s+(?:file\s+)?(?:named\s+|called\s+)?(?P<name>[\'\"][^\'\"]+[\'\"]|\S+)\s+(?:with\s+(?:content\s+|text\s+)?|containing\s+)(?P<content>.+)$",
            re.IGNORECASE,
        )
        match = followup_pattern.match(text)
        if match:
            name = self.clean_quotes(match.group("name"))
            content = self.clean_quotes(match.group("content"))
            return IntentResolution(
                status=IntentStatus.VALID,
                intent=CommandIntent(
                    "files.create", (directory_index, name, content)
                ),
                target=name,
                directory_index=directory_index,
            )

        # 3. Standard creation: "create [a] [new] file [named|called] <name> with [content] <content>".
        create_pattern_1 = re.compile(
            r"^(?:please\s+)?(?:create|make)\s+(?:a\s+)?(?:new\s+)?file\s+(?:named\s+|called\s+)?(?P<name>[\'\"][^\'\"]+[\'\"]|.+?)\s+(?:with\s+(?:content\s+|text\s+)?|containing\s+)(?P<content>.+)$",
            re.IGNORECASE,
        )
        match = create_pattern_1.match(text)
        if match:
            name = self.clean_quotes(match.group("name"))
            content = self.clean_quotes(match.group("content"))
            return IntentResolution(
                status=IntentStatus.VALID,
                intent=CommandIntent(
                    "files.create", (directory_index, name, content)
                ),
                target=name,
                directory_index=directory_index,
            )

        # 4. Inverted pattern: "write <content> to/in/into [file] <name>".
        write_pattern = re.compile(
            r"^(?:please\s+)?write\s+(?P<content>[\'\"][^\'\"]+[\'\"]|.+?)\s+(?:to|in|into)\s+(?:the\s+)?(?:file\s+)?(?P<name>[\'\"][^\'\"]+[\'\"]|\S+)$",
            re.IGNORECASE,
        )
        match = write_pattern.match(text)
        if match:
            name = self.clean_quotes(match.group("name"))
            content = self.clean_quotes(match.group("content"))
            return IntentResolution(
                status=IntentStatus.VALID,
                intent=CommandIntent(
                    "files.create", (directory_index, name, content)
                ),
                target=name,
                directory_index=directory_index,
            )

        # 5. Creation with empty content: "create file <name>".
        empty_create_pattern = re.compile(
            r"^(?:please\s+)?(?:create|make)\s+(?:a\s+)?(?:new\s+)?file\s+(?:named\s+|called\s+)?(?P<name>.+)$",
            re.IGNORECASE,
        )
        match = empty_create_pattern.match(text)
        if match:
            raw_name = match.group("name").strip()
            name = self.clean_quotes(raw_name)
            if name and name.casefold() not in {"file", "the file", "a file"}:
                return IntentResolution(
                    status=IntentStatus.VALID,
                    intent=CommandIntent(
                        "files.create", (directory_index, name, "")
                    ),
                    target=name,
                    directory_index=directory_index,
                )

        return None

    def _match_delete(
        self,
        text: str,
        directory_index: str,
        context: "ConversationContext",
    ) -> IntentResolution | None:
        """Match natural language patterns for deleting files."""
        # Pronoun reference: "delete it", "delete that file", "remove it", "erase it".
        pronoun_pattern = re.compile(
            r"^(?:please\s+)?(?:delete|remove|erase)\s+(?:it|that|that\s+file|this\s+file)$",
            re.IGNORECASE,
        )
        if pronoun_pattern.match(text):
            if context.has_target:
                return IntentResolution(
                    status=IntentStatus.VALID,
                    intent=CommandIntent(
                        "files.delete",
                        (context.last_directory_index, context.last_target),  # type: ignore
                    ),
                    target=context.last_target,
                    directory_index=context.last_directory_index,
                )
            return IntentResolution(
                status=IntentStatus.AMBIGUOUS,
                message=(
                    "Your request is ambiguous because no previous file was referenced. "
                    "Please specify the filename."
                ),
            )

        # Missing argument: "delete file", "delete a file", "remove file".
        missing_arg_patterns = (
            re.compile(
                r"^(?:please\s+)?(?:delete|remove|erase)\s+(?:the\s+|a\s+)?file$",
                re.IGNORECASE,
            ),
        )
        for pattern in missing_arg_patterns:
            if pattern.match(text):
                context.set_pending(
                    "files.delete",
                    "Which file would you like to delete? Please specify the filename.",
                    directory_index,
                )
                return IntentResolution(
                    status=IntentStatus.MISSING_ARGUMENT,
                    missing_argument_name="filename",
                    message="Which file would you like to delete? Please specify the filename.",
                    directory_index=directory_index,
                )

        # Explicit deletion.
        delete_pattern = re.compile(
            r"^(?:please\s+)?(?:delete|remove|erase)\s+(?:the\s+)?(?:file\s+)?(?P<name>.+)$",
            re.IGNORECASE,
        )
        match = delete_pattern.match(text)
        if match:
            raw_name = match.group("name").strip()
            name = self.clean_quotes(raw_name)
            if not name or name.casefold() in {"file", "the file", "a file"}:
                context.set_pending(
                    "files.delete",
                    "Which file would you like to delete? Please specify the filename.",
                    directory_index,
                )
                return IntentResolution(
                    status=IntentStatus.MISSING_ARGUMENT,
                    missing_argument_name="filename",
                    message="Which file would you like to delete? Please specify the filename.",
                    directory_index=directory_index,
                )

            return IntentResolution(
                status=IntentStatus.VALID,
                intent=CommandIntent("files.delete", (directory_index, name)),
                target=name,
                directory_index=directory_index,
            )

        return None
