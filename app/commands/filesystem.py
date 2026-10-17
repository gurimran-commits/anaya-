
import os
from pathlib import Path

from app.security.command_validator import resolve_safe_path


class FilesystemService:
    """Manage files directly inside explicitly authorized directories."""

    def __init__(
        self,
        allowed_directories: tuple[Path, ...],
        max_read_bytes: int = 1_000_000,
    ) -> None:
        if not allowed_directories:
            raise ValueError("At least one allowed directory is required")

        if max_read_bytes <= 0:
            raise ValueError("max_read_bytes must be positive")

        roots: list[Path] = []

        for directory in allowed_directories:
            root = Path(directory).resolve()

            if not root.is_dir():
                raise ValueError("Allowed directory must already exist")

            if self._is_protected_location(root):
                raise ValueError("System directories cannot be authorized")

            roots.append(root)

        if len(set(roots)) != len(roots):
            raise ValueError("Allowed directories must be unique")

        self._roots = tuple(roots)
        self._max_read_bytes = max_read_bytes

    @staticmethod
    def _protected_locations() -> tuple[Path, ...]:
        locations = [
            Path("/etc"),
            Path("/usr"),
            Path("/bin"),
            Path("/sbin"),
            Path("/lib"),
            Path("/lib64"),
            Path("/boot"),
            Path("/proc"),
            Path("/sys"),
            Path("/dev"),
            Path("/root"),
        ]

        for variable in (
            "WINDIR",
            "SystemRoot",
            "ProgramFiles",
            "ProgramFiles(x86)",
            "ProgramData",
        ):
            value = os.environ.get(variable)
            if value:
                locations.append(Path(value))

        return tuple(path.resolve() for path in locations)

    @classmethod
    def _is_protected_location(cls, path: Path) -> bool:
        """Reject system paths and roots that would expose system paths."""
        for protected in cls._protected_locations():
            if path == protected:
                return True

            if path.is_relative_to(protected):
                return True

            if protected.is_relative_to(path):
                return True

        return False

    def _root(self, directory_index: int) -> Path:
        if (
            not isinstance(directory_index, int)
            or isinstance(directory_index, bool)
            or not 0 <= directory_index < len(self._roots)
        ):
            raise ValueError("Invalid allowed-directory index")

        return self._roots[directory_index]

    def _file_path(self, directory_index: int, name: str) -> Path:
        root = self._root(directory_index)
        candidate = root / name

        # Do not follow a symlink, even when it points inside the root.
        if candidate.is_symlink():
            raise ValueError("Symbolic links are not allowed")

        safe_path = resolve_safe_path(root, name)

        if self._is_protected_location(safe_path):
            raise ValueError("System-file access is denied")

        return safe_path

    def list_files(self, directory_index: int = 0) -> tuple[str, ...]:
        """List ordinary, non-hidden files in an allowed directory."""
        root = self._root(directory_index)
        names = []

        for entry in root.iterdir():
            if entry.name.startswith(".") or entry.is_symlink():
                continue

            if not entry.is_file():
                continue

            try:
                self._file_path(directory_index, entry.name)
            except (ValueError, OSError):
                continue

            names.append(entry.name)

        return tuple(sorted(names, key=str.casefold))

    def read_text(self, directory_index: int, name: str) -> str:
        """Read a bounded-size UTF-8 text file."""
        path = self._file_path(directory_index, name)

        if not path.is_file():
            raise ValueError("File does not exist")

        if path.stat().st_size > self._max_read_bytes:
            raise ValueError("File exceeds the permitted size")

        with path.open("r", encoding="utf-8") as file:
            content = file.read(self._max_read_bytes + 1)

        if len(content.encode("utf-8")) > self._max_read_bytes:
            raise ValueError("File exceeds the permitted size")

        return content

    def create_text(
        self,
        directory_index: int,
        name: str,
        content: str,
    ) -> str:
        """Create a new UTF-8 text file without overwriting."""
        if not isinstance(content, str):
            raise ValueError("File content must be text")

        if len(content.encode("utf-8")) > self._max_read_bytes:
            raise ValueError("Content exceeds the permitted size")

        path = self._file_path(directory_index, name)

        # Exclusive creation refuses to overwrite an existing file.
        with path.open("x", encoding="utf-8", newline="") as file:
            file.write(content)

        return f"Created {path.name}"

    def delete_file(self, directory_index: int, name: str) -> str:
        """Delete a file. Expose only through a confirmation-protected command."""
        path = self._file_path(directory_index, name)

        if not path.is_file():
            raise ValueError("File does not exist")

        path.unlink()
        return f"Deleted {path.name}"
    
    @property
    def directory_count(self) -> int:
        """Return the number of explicitly authorized directories."""
        return len(self._roots)

    def validate_filename(self, directory_index: int, name: str) -> bool:
        """Check whether a filename is permitted in a specific directory."""
        try:
            self._file_path(directory_index, name)
            return True
        except (TypeError, ValueError, OSError):
            return False
