class ConversationContext:
    """Bounded, short-term conversational context for turn continuity.

    Tracks the immediate prior target, command, and any pending clarification.
    Never persists across application sessions and can be reset at any time.
    """

    def __init__(self) -> None:
        self.last_command: str | None = None
        self.last_target: str | None = None
        self.last_directory_index: str = "0"

        self.pending_intent: str | None = None
        self.pending_prompt: str | None = None
        self.pending_directory_index: str = "0"
        self.pending_argument_name: str | None = None

    def update(
        self,
        command: str,
        target: str | None = None,
        directory_index: str = "0",
    ) -> None:
        """Update context after a successful command execution or approval request."""
        self.last_command = command
        if target is not None:
            self.last_target = target
        self.last_directory_index = directory_index
        self.clear_pending()

    def set_pending(
        self,
        intent: str,
        prompt: str,
        directory_index: str = "0",
        argument_name: str = "filename",
    ) -> None:
        """Record an intent waiting for missing argument clarification."""
        self.pending_intent = intent
        self.pending_prompt = prompt
        self.pending_directory_index = directory_index
        self.pending_argument_name = argument_name

    def clear_pending(self) -> None:
        """Clear any waiting clarification request."""
        self.pending_intent = None
        self.pending_prompt = None
        self.pending_directory_index = "0"
        self.pending_argument_name = None

    def reset(self) -> None:
        """Completely reset all conversational state."""
        self.last_command = None
        self.last_target = None
        self.last_directory_index = "0"
        self.clear_pending()

    @property
    def has_pending(self) -> bool:
        """Return True if an intent is currently waiting for clarification."""
        return self.pending_intent is not None

    @property
    def has_target(self) -> bool:
        """Return True if a valid prior target exists in context."""
        return self.last_target is not None
