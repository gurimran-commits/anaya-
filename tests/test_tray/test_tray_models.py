"""Unit tests for system tray models, tooltip Unicode truncation, and status snapshots."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from unittest.mock import MagicMock

import pytest

from app.tray.models import (
    TrayServiceState,
    TrayStatusSnapshot,
    capture_status_snapshot,
    sanitize_and_truncate_tooltip,
)


class TestTooltipUnicodeTruncation:
    """Test suite verifying safe UTF-16 code unit truncation and surrogate pair handling."""

    def test_short_string_preserved(self) -> None:
        """Strings well within the 127 code-unit budget are preserved unmodified."""
        text = "Anaya AI Assistant"
        truncated = sanitize_and_truncate_tooltip(text)
        assert truncated == text

    def test_exact_limit_preserved(self) -> None:
        """Strings exactly at 127 ASCII characters are preserved."""
        text = "A" * 127
        truncated = sanitize_and_truncate_tooltip(text)
        assert truncated == text
        assert len(truncated.encode("utf-16-le")) // 2 == 127

    def test_ascii_overflow_truncated_to_exact_limit(self) -> None:
        """Strings exceeding 127 characters are truncated cleanly."""
        text = "B" * 200
        truncated = sanitize_and_truncate_tooltip(text)
        assert len(truncated) == 127
        assert len(truncated.encode("utf-16-le")) // 2 == 127
        assert truncated == "B" * 127

    def test_surrogate_pair_crossing_boundary_not_split(self) -> None:
        """Surrogate pair (emoji) crossing the 127 code unit boundary is dropped cleanly."""
        # 126 ASCII chars (126 code units) + 1 emoji (2 code units) = 128 code units.
        # Max code units is 127, which would split the emoji. The high surrogate must be dropped.
        emoji = "🎙"
        text = ("C" * 126) + emoji
        assert len(text.encode("utf-16-le")) // 2 == 128

        truncated = sanitize_and_truncate_tooltip(text, max_code_units=127)
        # Slicing at 127 would leave a dangling high surrogate. The function drops it, leaving 126 units.
        assert len(truncated.encode("utf-16-le")) // 2 == 126
        assert truncated == "C" * 126
        # Must decode back to clean string without UnicodeDecodeError
        assert truncated.encode("utf-16-le").decode("utf-16-le") == truncated

    def test_emoji_within_budget_preserved(self) -> None:
        """Emoji within budget are preserved."""
        text = "Anaya 🤖 Assistant"
        truncated = sanitize_and_truncate_tooltip(text)
        assert truncated == text
        assert "🤖" in truncated

    def test_whitespace_and_empty_string(self) -> None:
        """Empty string or whitespace handled cleanly."""
        assert sanitize_and_truncate_tooltip("") == ""
        assert sanitize_and_truncate_tooltip("   ") == ""


class TestTrayStatusSnapshot:
    """Test suite for TrayStatusSnapshot immutability and capture behavior."""

    def test_snapshot_immutability(self) -> None:
        """TrayStatusSnapshot is frozen and cannot be modified."""
        snap = TrayStatusSnapshot(
            controller_running=True,
            hotkey_running=True,
            hotkey_state="RUNNING",
            hotkey_combination="win+alt+a",
            voice_active=False,
            speech_available=True,
            ai_enabled=True,
            ai_profile="balanced",
        )
        with pytest.raises(FrozenInstanceError):
            snap.controller_running = False  # type: ignore[misc]

    def test_capture_status_snapshot_with_mocks(self) -> None:
        """Capture point-in-time snapshot safely from mock controller and hotkey service."""
        mock_ctrl = MagicMock()
        mock_ctrl.is_running = True
        mock_ctrl.voice_pipeline = MagicMock()
        mock_ctrl.model_manager.is_enabled = True
        mock_ctrl.model_manager.active_profile.value = "balanced"

        mock_hk = MagicMock()
        mock_hk.is_running = True
        mock_hk.state.value = "RUNNING"
        mock_hk.combination.canonical_string = "win+alt+a"
        mock_hk.is_voice_active = False

        snap = capture_status_snapshot(mock_ctrl, mock_hk)
        assert snap.controller_running is True
        assert snap.hotkey_running is True
        assert snap.hotkey_state == "RUNNING"
        assert snap.hotkey_combination == "win+alt+a"
        assert snap.voice_active is False
        assert snap.speech_available is True
        assert snap.ai_enabled is True
        assert snap.ai_profile == "balanced"

    def test_capture_status_snapshot_with_none(self) -> None:
        """Capture snapshot handles None inputs with safe defaults."""
        snap = capture_status_snapshot(None, None)
        assert snap.controller_running is False
        assert snap.hotkey_running is False
        assert snap.hotkey_state == "UNAVAILABLE"
        assert snap.hotkey_combination == "None"
        assert snap.voice_active is False
        assert snap.speech_available is False
        assert snap.ai_enabled is False
        assert snap.ai_profile == "none"

    def test_service_state_enum_values(self) -> None:
        """Verify explicit lifecycle state values."""
        assert TrayServiceState.STOPPED == "STOPPED"
        assert TrayServiceState.STARTING == "STARTING"
        assert TrayServiceState.RUNNING == "RUNNING"
        assert TrayServiceState.STOPPING == "STOPPING"
        assert TrayServiceState.TIMEOUT_STOPPED == "TIMEOUT_STOPPED"
