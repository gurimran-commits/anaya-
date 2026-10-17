
import os
from pathlib import Path, PureWindowsPath


_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def resolve_safe_path(base: Path, name: str) -> Path:
    """Resolve a plain filename while rejecting unsafe path forms."""
    if not isinstance(name, str) or not name:
        raise ValueError("Filename must not be empty")

    if len(name) > 255:
        raise ValueError("Filename is too long")

    if name.startswith("."):
        raise ValueError("Hidden filenames are not allowed")

    if name in {".", ".."} or ".." in name:
        raise ValueError("Path traversal is not allowed")

    if "/" in name or "\\" in name:
        raise ValueError("Path separators are not allowed")

    if ":" in name:
        raise ValueError("Drive prefixes and alternate data streams are not allowed")

    if any(ord(character) < 32 or ord(character) == 127 for character in name):
        raise ValueError("Control characters are not allowed")

    if name.endswith((" ", ".")):
        raise ValueError("Trailing spaces and dots are not allowed")

    if any(character in '<>:"|?*' for character in name):
        raise ValueError("Filename contains an unsupported character")

    windows_name = PureWindowsPath(name)
    if windows_name.is_absolute() or windows_name.drive:
        raise ValueError("Absolute and drive-relative paths are not allowed")

    reserved_stem = name.split(".", 1)[0].upper()
    if reserved_stem in _WINDOWS_RESERVED_NAMES:
        raise ValueError("Reserved Windows device names are not allowed")

    root = Path(base).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Base path must be a directory")

    candidate = root / name

    # Resolve existing symlinks and reject targets outside the root.
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("Path escapes the permitted directory") from exc

    return resolved
