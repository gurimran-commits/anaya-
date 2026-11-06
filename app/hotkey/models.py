"""Data models, constants, and exceptions for global hotkey handling."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

# Standard Win32 modifier key constants
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

# Standard Windows message codes
WM_QUIT = 0x0012
WM_HOTKEY = 0x0312

# Canonical modifier order and alias map
_MODIFIER_ALIASES: dict[str, tuple[str, int]] = {
    "win": ("win", MOD_WIN),
    "alt": ("alt", MOD_ALT),
    "ctrl": ("ctrl", MOD_CONTROL),
    "control": ("ctrl", MOD_CONTROL),
    "shift": ("shift", MOD_SHIFT),
}

_CANONICAL_MODIFIER_ORDER = ["win", "ctrl", "alt", "shift"]

# Virtual key map for special keys
_SPECIAL_KEYS: dict[str, int] = {
    "space": 0x20,
    "enter": 0x0D,
    "return": 0x0D,
}
for i in range(1, 13):
    _SPECIAL_KEYS[f"f{i}"] = 0x70 + (i - 1)


class HotkeyServiceState(str, Enum):
    """Explicit lifecycle states for GlobalHotkeyService."""

    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    REGISTRATION_FAILED = "REGISTRATION_FAILED"
    PUMP_FAILED = "PUMP_FAILED"
    TIMEOUT_STOPPED = "TIMEOUT_STOPPED"


class HotkeyError(Exception):
    """Base exception for all global hotkey errors."""


class HotkeyConfigurationError(HotkeyError, ValueError):
    """Raised when a hotkey combination or setting is invalid."""


class HotkeyRegistrationError(HotkeyError):
    """Raised when hotkey registration fails on the host OS."""


class HotkeyConflictError(HotkeyRegistrationError):
    """Raised specifically when the requested hotkey conflicts with another application (Win32 1409)."""


@dataclass(frozen=True)
class HotkeyCombination:
    """Immutable parsed representation of a keyboard shortcut.

    Attributes:
        modifiers: Canonical modifier names (e.g. ('win', 'alt')).
        key: Canonical key identifier (e.g. 'a', 'space', 'f1').
        vk_code: Virtual key code.
        fs_modifiers: Bitwise OR of MOD_* constants including MOD_NOREPEAT.
        canonical_string: Standardized string representation (e.g. 'win+alt+a').
    """

    modifiers: tuple[str, ...]
    key: str
    vk_code: int
    fs_modifiers: int
    canonical_string: str

    @classmethod
    def parse(cls, combination_str: str) -> HotkeyCombination:
        """Parse a case-insensitive shortcut string into an immutable HotkeyCombination.

        Examples:
            'win+alt+a' -> HotkeyCombination(modifiers=('win', 'alt'), key='a', ...)
            'Ctrl+Shift+Space' -> HotkeyCombination(modifiers=('ctrl', 'shift'), key='space', ...)
            'Alt+F12' -> HotkeyCombination(modifiers=('alt',), key='f12', ...)

        Raises:
            HotkeyConfigurationError: On empty input, missing keys, duplicate modifiers,
                unknown tokens, multiple keys, or missing modifiers.
        """
        if not isinstance(combination_str, str):
            raise HotkeyConfigurationError(
                f"Hotkey combination must be a string, got {type(combination_str).__name__}"
            )

        stripped = combination_str.strip()
        if not stripped:
            raise HotkeyConfigurationError("Hotkey combination cannot be empty or whitespace.")

        parts = [p.strip().lower() for p in stripped.split("+") if p.strip()]
        if not parts:
            raise HotkeyConfigurationError("Hotkey combination cannot be empty.")

        seen_modifiers: set[str] = set()
        parsed_modifiers: list[tuple[str, int]] = []
        parsed_key: str | None = None
        parsed_vk: int | None = None

        for token in parts:
            if token in _MODIFIER_ALIASES:
                canon_mod, mod_val = _MODIFIER_ALIASES[token]
                if canon_mod in seen_modifiers:
                    raise HotkeyConfigurationError(
                        f"Duplicate modifier '{token}' in combination '{combination_str}'."
                    )
                seen_modifiers.add(canon_mod)
                parsed_modifiers.append((canon_mod, mod_val))
            else:
                # Key token
                if parsed_key is not None:
                    raise HotkeyConfigurationError(
                        f"Multiple keys specified in '{combination_str}': '{parsed_key}' and '{token}'."
                    )

                vk = cls._resolve_virtual_key(token)
                if vk is None:
                    raise HotkeyConfigurationError(
                        f"Unknown or unsupported key token '{token}' in '{combination_str}'."
                    )
                parsed_key = "enter" if token in ("enter", "return") else token
                parsed_vk = vk

        if parsed_key is None or parsed_vk is None:
            raise HotkeyConfigurationError(
                f"Missing key in hotkey combination '{combination_str}'. At least one key must be specified."
            )

        if not parsed_modifiers:
            raise HotkeyConfigurationError(
                f"Missing modifier in hotkey combination '{combination_str}'. "
                "At least one modifier (e.g. 'alt', 'ctrl', 'win', 'shift') is required."
            )

        # Sort modifiers according to canonical order
        sorted_modifiers = sorted(
            parsed_modifiers,
            key=lambda item: _CANONICAL_MODIFIER_ORDER.index(item[0]),
        )
        mod_names = tuple(item[0] for item in sorted_modifiers)

        # Combine modifier bits and apply MOD_NOREPEAT automatically
        fs_bits = 0
        for _, bit in sorted_modifiers:
            fs_bits |= bit
        fs_bits |= MOD_NOREPEAT

        canonical_str = "+".join(mod_names) + f"+{parsed_key}"

        return cls(
            modifiers=mod_names,
            key=parsed_key,
            vk_code=parsed_vk,
            fs_modifiers=fs_bits,
            canonical_string=canonical_str,
        )

    @staticmethod
    def _resolve_virtual_key(token: str) -> int | None:
        """Resolve a single key token to its virtual key code."""
        if len(token) == 1:
            c = token.upper()
            if "A" <= c <= "Z":
                return ord(c)
            if "0" <= c <= "9":
                return ord(c)
            return None

        return _SPECIAL_KEYS.get(token)
