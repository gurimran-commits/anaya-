"""Bounded in-memory conversation history for multi-turn conversational AI."""

from __future__ import annotations

from app.ai.models import ChatMessage


class ConversationHistory:
    """Chronological, bounded in-memory chat history for conversational continuity.

    Strictly isolated from persistent Memory Engine storage.
    """

    DEFAULT_SYSTEM_PROMPT = (
        "You are Anaya, a helpful, lightweight personal voice and desktop assistant. "
        "Keep your answers concise, accurate, and conversational."
    )

    def __init__(
        self,
        max_turns: int = 10,
        system_prompt: str | None = None,
    ) -> None:
        if max_turns <= 0:
            raise ValueError("max_turns must be a positive integer.")
        self.max_turns = max_turns
        self.system_prompt = system_prompt if system_prompt is not None else self.DEFAULT_SYSTEM_PROMPT
        self._messages: list[ChatMessage] = []

    def add_user_message(self, content: str) -> None:
        """Append a user message and maintain turn bounds."""
        clean = content.strip()
        if not clean:
            return
        self._messages.append(ChatMessage(role="user", content=clean))
        self._enforce_bounds()

    def add_assistant_message(self, content: str) -> None:
        """Append an assistant response and maintain turn bounds."""
        clean = content.strip()
        if not clean:
            return
        self._messages.append(ChatMessage(role="assistant", content=clean))
        self._enforce_bounds()

    def set_system_prompt(self, prompt: str | None) -> None:
        """Update or clear the system prompt without losing conversational messages."""
        self.system_prompt = prompt

    def get_messages(self) -> list[ChatMessage]:
        """Return the complete sequence of messages, prepending system prompt if present."""
        result: list[ChatMessage] = []
        if self.system_prompt and self.system_prompt.strip():
            result.append(ChatMessage(role="system", content=self.system_prompt.strip()))
        result.extend(self._messages)
        return result

    def clear(self) -> None:
        """Clear all conversation turns while preserving system prompt."""
        self._messages.clear()

    def pop_last_user_message(self) -> None:
        """Remove the most recent message if it was a user message (e.g. after generation failure)."""
        if self._messages and self._messages[-1].role == "user":
            self._messages.pop()

    @property
    def turn_count(self) -> int:
        """Return the number of user/assistant messages stored."""
        return len(self._messages)

    def _enforce_bounds(self) -> None:
        """Trim oldest messages when the turn count exceeds 2 * max_turns (user+assistant pairs)."""
        max_messages = self.max_turns * 2
        if len(self._messages) > max_messages:
            # Drop the oldest messages to stay within max_messages
            overflow = len(self._messages) - max_messages
            self._messages = self._messages[overflow:]
