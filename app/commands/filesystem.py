
import os
import stat
from pathlib import Path

from app.security.command_validator import resolve_safe_path


class FilesystemService:
    """Manage files inside explicitly authorized directories."""

    def __init__(
        self,
        allowed_directories: tuple[Path, ...],
        max_read_bytes: int = 1_000_000,
    ) -> None:
        if not allowed_directories:
            raise ValueError("At least one allowed directory is required")
        if (
            not isinstance(max_read_bytes, int)
            or isinstance(max_read_bytes, bool)
            or max_read_bytes <= 0
        ):
            raise ValueError("max_read_bytes must be a positive integer")

        roots = []
        for directory in allowed_directories:
            root = Path(directory).resolve(strict=True)
            if not root.is_dir():
                raise ValueError("Allowed directory must already exist")
            if self._is_protected_location(root):
                raise ValueError("System directories cannot be authorized")
            roots.append(root)

        if len(set(roots)) != len(roots):
            raise ValueError("Allowed directories must be unique")

        self._roots = tuple(roots)
        self._max_read_bytes = max_read_bytes
        self._root_fds: list[int] = []
        self._closed = False

        self._descriptor_mode = (
            os.name == "posix"
            and all(
                hasattr(os, name)
                for name in ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC")
            )
            and os.open in os.supports_dir_fd
        )

        if self._descriptor_mode:
            try:
                for root in self._roots:
                    fd = os.open(
                        root,
                        os.O_RDONLY
                        | os.O_DIRECTORY
                        | os.O_NOFOLLOW
                        | os.O_CLOEXEC,
                    )
                    if not stat.S_ISDIR(os.fstat(fd).st_mode):
                        os.close(fd)
                        raise ValueError(
                            "Authorized root is not a directory"
                        )
                    self._root_fds.append(fd)
            except Exception:
                self.close()
                raise

    @staticmethod
    def _protected_locations() -> tuple[Path, ...]:
        locations = [
            Path("/etc"), Path("/usr"), Path("/bin"), Path("/sbin"),
            Path("/lib"), Path("/lib64"), Path("/boot"), Path("/proc"),
            Path("/sys"), Path("/dev"), Path("/root"),
        ]
        for variable in (
            "WINDIR", "SystemRoot", "ProgramFiles",
            "ProgramFiles(x86)", "ProgramData",
        ):
            value = os.environ.get(variable)
            if value:
                locations.append(Path(value))
        return tuple(path.resolve() for path in locations)

    @classmethod
    def _is_protected_location(cls, path: Path) -> bool:
        for protected in cls._protected_locations():
            if (
                path == protected
                or path.is_relative_to(protected)
                or protected.is_relative_to(path)
            ):
                return True
        return False

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("FilesystemService is closed")

    def close(self) -> None:
        """Permanently close the service; safe to call repeatedly."""
        if self._closed:
            return

        self._closed = True
        while self._root_fds:
            fd = self._root_fds.pop()
            try:
                os.close(fd)
            except OSError:
                pass

    def __enter__(self):
        self._ensure_open()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def _root(self, directory_index: int) -> Path:
        self._ensure_open()
        if (
            not isinstance(directory_index, int)
            or isinstance(directory_index, bool)
            or not 0 <= directory_index < len(self._roots)
        ):
            raise ValueError("Invalid allowed-directory index")
        return self._roots[directory_index]

    def _name(self, directory_index: int, name: str) -> str:
        self._root(directory_index)
        resolve_safe_path(self._roots[directory_index], name)
        return name

    def _open_file(self, directory_index: int, name: str, flags: int) -> int:
        name = self._name(directory_index, name)

        if self._descriptor_mode:
            self._ensure_open()
            return os.open(
                name,
                flags | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=self._root_fds[directory_index],
            )

        path = self._roots[directory_index] / name
        if path.is_symlink():
            raise ValueError("Symbolic links are not allowed")
        return os.open(path, flags)

    def list_files(self, directory_index: int = 0) -> tuple[str, ...]:
        root = self._root(directory_index)
        names = []

        for entry in root.iterdir():
            self._ensure_open()
            if entry.name.startswith(".") or entry.is_symlink():
                continue
            try:
                fd = self._open_file(
                    directory_index, entry.name, os.O_RDONLY
                )
                try:
                    if stat.S_ISREG(os.fstat(fd).st_mode):
                        names.append(entry.name)
                finally:
                    os.close(fd)
            except (ValueError, OSError):
                continue

        self._ensure_open()
        return tuple(sorted(names, key=str.casefold))

    def read_text(self, directory_index: int, name: str) -> str:
        fd = self._open_file(directory_index, name, os.O_RDONLY)
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("File does not exist")
            if metadata.st_size > self._max_read_bytes:
                raise ValueError("File exceeds the permitted size")

            chunks = []
            total = 0
            while True:
                chunk = os.read(
                    fd, min(65536, self._max_read_bytes + 1 - total)
                )
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > self._max_read_bytes:
                    raise ValueError("File exceeds the permitted size")

            return b"".join(chunks).decode("utf-8")
        finally:
            os.close(fd)

    def create_text(
        self,
        directory_index: int,
        name: str,
        content: str,
    ) -> str:
        self._ensure_open()
        if not isinstance(content, str):
            raise ValueError("File content must be text")

        encoded = content.encode("utf-8")
        if len(encoded) > self._max_read_bytes:
            raise ValueError("Content exceeds the permitted size")

        fd = self._open_file(
            directory_index,
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        )
        try:
            with os.fdopen(fd, "wb") as file:
                fd = -1
                file.write(encoded)
        finally:
            if fd >= 0:
                os.close(fd)

        return f"Created {name}"

    def delete_file(self, directory_index: int, name: str) -> str:
        name = self._name(directory_index, name)

        if self._descriptor_mode and os.unlink in os.supports_dir_fd:
            self._ensure_open()
            root_fd = self._root_fds[directory_index]
            try:
                metadata = os.stat(
                    name, dir_fd=root_fd, follow_symlinks=False
                )
            except FileNotFoundError as exc:
                raise ValueError("File does not exist") from exc

            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("Only regular files can be deleted")

            os.unlink(name, dir_fd=root_fd)
        else:
            path = self._roots[directory_index] / name
            if path.is_symlink():
                raise ValueError("Symbolic links are not allowed")
            if not path.is_file():
                raise ValueError("File does not exist")
            path.unlink()

        return f"Deleted {name}"

    @property
    def directory_count(self) -> int:
        self._ensure_open()
        return len(self._roots)

    def validate_filename(self, directory_index: int, name: str) -> bool:
        try:
            self._name(directory_index, name)
            return True
        except (TypeError, ValueError, OSError, RuntimeError):
            return False
