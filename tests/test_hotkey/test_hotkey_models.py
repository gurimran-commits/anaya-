"""Unit tests for HotkeyCombination, parsing, models, and AppConfig integration."""

from __future__ import annotations

import math
from dataclasses import FrozenInstanceError

import pytest

from app.core.config import AppConfig, DEFAULT_CONFIG
from app.hotkey.models import (
    MOD_ALT,
    MOD_CONTROL,
    MOD_NOREPEAT,
    MOD_SHIFT,
    MOD_WIN,
    HotkeyCombination,
    HotkeyConfigurationError,
    HotkeyServiceState,
)


class TestHotkeyCombinationParsing:
    """Test suite for parsing and validating keyboard shortcut strings."""

    def test_parse_valid_default_combination(self) -> None:
        """Verify parsing of default 'win+alt+a' combination."""
        combo = HotkeyCombination.parse("win+alt+a")
        assert combo.modifiers == ("win", "alt")
        assert combo.key == "a"
        assert combo.vk_code == ord("A")
        assert combo.canonical_string == "win+alt+a"
        assert combo.fs_modifiers & MOD_WIN
        assert combo.fs_modifiers & MOD_ALT
        assert combo.fs_modifiers & MOD_NOREPEAT

    def test_parse_case_insensitivity(self) -> None:
        """Verify case-insensitive parsing and normalization."""
        combo1 = HotkeyCombination.parse("WIN+ALT+A")
        combo2 = HotkeyCombination.parse("wIn+AlT+a")
        combo3 = HotkeyCombination.parse("  Alt+Win+A  ")

        assert combo1.canonical_string == "win+alt+a"
        assert combo2.canonical_string == "win+alt+a"
        assert combo3.canonical_string == "win+alt+a"
        assert combo1 == combo2 == combo3

    def test_parse_control_and_shift_aliases(self) -> None:
        """Verify 'ctrl', 'control', and 'shift' modifiers."""
        combo_ctrl = HotkeyCombination.parse("ctrl+shift+space")
        combo_control = HotkeyCombination.parse("control+shift+space")

        assert combo_ctrl.modifiers == ("ctrl", "shift")
        assert combo_ctrl.key == "space"
        assert combo_ctrl.vk_code == 0x20
        assert combo_ctrl.canonical_string == "ctrl+shift+space"
        assert combo_ctrl == combo_control
        assert combo_ctrl.fs_modifiers & MOD_CONTROL
        assert combo_ctrl.fs_modifiers & MOD_SHIFT
        assert combo_ctrl.fs_modifiers & MOD_NOREPEAT

    def test_parse_function_keys_and_digits(self) -> None:
        """Verify function keys F1-F12 and digits 0-9."""
        combo_f1 = HotkeyCombination.parse("alt+f1")
        assert combo_f1.key == "f1"
        assert combo_f1.vk_code == 0x70

        combo_f12 = HotkeyCombination.parse("win+f12")
        assert combo_f12.key == "f12"
        assert combo_f12.vk_code == 0x7B

        combo_digit = HotkeyCombination.parse("ctrl+alt+5")
        assert combo_digit.key == "5"
        assert combo_digit.vk_code == ord("5")

    def test_parse_enter_and_return_aliases(self) -> None:
        """Verify 'enter' and 'return' keys resolve to VK_RETURN."""
        combo_enter = HotkeyCombination.parse("ctrl+enter")
        combo_return = HotkeyCombination.parse("ctrl+return")

        assert combo_enter.key == "enter"
        assert combo_enter.vk_code == 0x0D
        assert combo_enter.canonical_string == "ctrl+enter"
        assert combo_return.canonical_string == "ctrl+enter"
        assert combo_enter == combo_return

    def test_rejection_missing_key(self) -> None:
        """Reject combinations specifying only modifiers without a key."""
        with pytest.raises(HotkeyConfigurationError, match="Missing key"):
            HotkeyCombination.parse("win+alt")

        with pytest.raises(HotkeyConfigurationError, match="Missing key"):
            HotkeyCombination.parse("ctrl+shift+")

    def test_rejection_missing_modifier(self) -> None:
        """Reject bare keys without any modifier for safety."""
        with pytest.raises(HotkeyConfigurationError, match="Missing modifier"):
            HotkeyCombination.parse("a")

        with pytest.raises(HotkeyConfigurationError, match="Missing modifier"):
            HotkeyCombination.parse("space")

    def test_rejection_duplicate_modifiers(self) -> None:
        """Reject duplicate modifiers in combinations."""
        with pytest.raises(HotkeyConfigurationError, match="Duplicate modifier"):
            HotkeyCombination.parse("alt+alt+a")

        with pytest.raises(HotkeyConfigurationError, match="Duplicate modifier"):
            HotkeyCombination.parse("ctrl+control+a")

        with pytest.raises(HotkeyConfigurationError, match="Duplicate modifier"):
            HotkeyCombination.parse("win+ctrl+win+a")

    def test_rejection_multiple_keys(self) -> None:
        """Reject combinations specifying multiple keys."""
        with pytest.raises(HotkeyConfigurationError, match="Multiple keys"):
            HotkeyCombination.parse("win+alt+a+b")

        with pytest.raises(HotkeyConfigurationError, match="Multiple keys"):
            HotkeyCombination.parse("ctrl+space+enter")

    def test_rejection_unknown_tokens(self) -> None:
        """Reject unknown key or modifier tokens."""
        with pytest.raises(HotkeyConfigurationError, match="Unknown or unsupported"):
            HotkeyCombination.parse("win+alt+invalidkey")

        with pytest.raises(HotkeyConfigurationError, match="Unknown or unsupported"):
            HotkeyCombination.parse("super+alt+a")

    def test_rejection_empty_or_invalid_types(self) -> None:
        """Reject empty strings, whitespace, and non-string inputs."""
        with pytest.raises(HotkeyConfigurationError, match="cannot be empty"):
            HotkeyCombination.parse("")

        with pytest.raises(HotkeyConfigurationError, match="cannot be empty"):
            HotkeyCombination.parse("   ")

        with pytest.raises(HotkeyConfigurationError, match="must be a string"):
            HotkeyCombination.parse(123)  # type: ignore[arg-type]

        with pytest.raises(HotkeyConfigurationError, match="must be a string"):
            HotkeyCombination.parse(None)  # type: ignore[arg-type]

    def test_hotkey_combination_immutability(self) -> None:
        """Verify HotkeyCombination is frozen and cannot be modified."""
        combo = HotkeyCombination.parse("win+alt+a")
        with pytest.raises(FrozenInstanceError):
            combo.key = "b"  # type: ignore[misc]


class TestAppConfigHotkeyIntegration:
    """Test suite for hotkey configuration fields and AppConfig validation."""

    def test_default_config_hotkey_values(self) -> None:
        """Verify default AppConfig has hotkey disabled and default shortcut."""
        config = DEFAULT_CONFIG
        assert config.hotkey_enabled is False
        assert config.hotkey_combination == "win+alt+a"
        assert config.hotkey_voice_duration == 5.0

    def test_custom_valid_hotkey_config(self) -> None:
        """Verify creating AppConfig with custom valid hotkey settings."""
        config = AppConfig(
            hotkey_enabled=True,
            hotkey_combination="Ctrl+Shift+Space",
            hotkey_voice_duration=8.5,
        )
        assert config.hotkey_enabled is True
        assert config.hotkey_combination == "ctrl+shift+space"
        assert config.hotkey_voice_duration == 8.5

    def test_app_config_rejects_non_boolean_hotkey_enabled(self) -> None:
        """Reject non-boolean hotkey_enabled values."""
        with pytest.raises(TypeError, match="hotkey_enabled must be a boolean"):
            AppConfig(hotkey_enabled="true")  # type: ignore[arg-type]

        with pytest.raises(TypeError, match="hotkey_enabled must be a boolean"):
            AppConfig(hotkey_enabled=1)  # type: ignore[arg-type]

    def test_app_config_rejects_invalid_hotkey_voice_duration(self) -> None:
        """Reject boolean, non-numeric, non-finite, and out-of-bounds durations."""
        # Booleans must not pass as numeric
        with pytest.raises(TypeError, match="hotkey_voice_duration must be a float or int"):
            AppConfig(hotkey_voice_duration=True)  # type: ignore[arg-type]

        # Non-numeric
        with pytest.raises(TypeError, match="hotkey_voice_duration must be a float or int"):
            AppConfig(hotkey_voice_duration="5.0")  # type: ignore[arg-type]

        # Non-finite
        with pytest.raises(ValueError, match="must be a finite number"):
            AppConfig(hotkey_voice_duration=math.inf)

        with pytest.raises(ValueError, match="must be a finite number"):
            AppConfig(hotkey_voice_duration=math.nan)

        # Non-positive or zero
        with pytest.raises(ValueError, match="must be positive"):
            AppConfig(hotkey_voice_duration=0.0)

        with pytest.raises(ValueError, match="must be positive"):
            AppConfig(hotkey_voice_duration=-1.5)

        # Exceeds max bound (60.0s)
        with pytest.raises(ValueError, match="at most 60.0s"):
            AppConfig(hotkey_voice_duration=60.1)

    def test_app_config_rejects_invalid_hotkey_combination(self) -> None:
        """Reject non-string or invalid syntax hotkey_combination values."""
        with pytest.raises(TypeError, match="hotkey_combination must be a string"):
            AppConfig(hotkey_combination=123)  # type: ignore[arg-type]

        with pytest.raises(HotkeyConfigurationError):
            AppConfig(hotkey_combination="invalid+combo")

    def test_app_config_immutability(self) -> None:
        """Verify AppConfig remains frozen."""
        config = AppConfig()
        with pytest.raises(FrozenInstanceError):
            config.hotkey_enabled = True  # type: ignore[misc]
