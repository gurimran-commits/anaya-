"""Unit tests for in-memory ConversationHistory buffer."""

import pytest

from app.ai.history import ConversationHistory


class TestConversationHistory:
    """Test suite for ConversationHistory bounded multi-turn buffer."""

    def test_chronological_ordering(self):
        hist = ConversationHistory(max_turns=5, system_prompt="You are Anaya.")
        hist.add_user_message("Hello")
        hist.add_assistant_message("Hi there")
        hist.add_user_message("How are you?")

        msgs = hist.get_messages()
        assert len(msgs) == 4
        assert msgs[0].role == "system"
        assert msgs[0].content == "You are Anaya."
        assert msgs[1].role == "user"
        assert msgs[1].content == "Hello"
        assert msgs[2].role == "assistant"
        assert msgs[2].content == "Hi there"
        assert msgs[3].role == "user"
        assert msgs[3].content == "How are you?"

    def test_turn_bounds_and_oldest_trimming(self):
        # max_turns=2 -> at most 4 user/assistant messages
        hist = ConversationHistory(max_turns=2, system_prompt="SysPrompt")

        # Turn 1
        hist.add_user_message("Q1")
        hist.add_assistant_message("A1")
        # Turn 2
        hist.add_user_message("Q2")
        hist.add_assistant_message("A2")

        msgs = hist.get_messages()
        assert len(msgs) == 5  # system + 4 turns

        # Turn 3 exceeds bounds -> oldest messages (Q1, A1) are trimmed
        hist.add_user_message("Q3")
        hist.add_assistant_message("A3")

        msgs2 = hist.get_messages()
        assert len(msgs2) == 5  # system + Q2, A2, Q3, A3
        assert msgs2[0].role == "system"
        assert msgs2[0].content == "SysPrompt"
        assert msgs2[1].content == "Q2"
        assert msgs2[2].content == "A2"
        assert msgs2[3].content == "Q3"
        assert msgs2[4].content == "A3"

    def test_clear_preserves_system_prompt(self):
        hist = ConversationHistory(max_turns=5, system_prompt="System instructions")
        hist.add_user_message("Hello")
        hist.add_assistant_message("Greetings")
        assert hist.turn_count == 2

        hist.clear()
        assert hist.turn_count == 0
        msgs = hist.get_messages()
        assert len(msgs) == 1
        assert msgs[0].role == "system"
        assert msgs[0].content == "System instructions"

    def test_invalid_max_turns_rejected(self):
        with pytest.raises(ValueError, match="positive integer"):
            ConversationHistory(max_turns=0)

        with pytest.raises(ValueError, match="positive integer"):
            ConversationHistory(max_turns=-1)
