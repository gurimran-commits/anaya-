
import pytest

from app.commands.filesystem import FilesystemService


@pytest.fixture
def filesystem(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()

    return FilesystemService((allowed,)), allowed


def test_lists_files_in_allowed_directory(filesystem):
    service, root = filesystem
    (root / "notes.txt").write_text("hello", encoding="utf-8")

    assert service.list_files() == ("notes.txt",)


def test_reads_text_file(filesystem):
    service, root = filesystem
    (root / "notes.txt").write_text("hello Anaya", encoding="utf-8")

    assert service.read_text(0, "notes.txt") == "hello Anaya"


def test_creates_file_without_overwriting(filesystem):
    service, root = filesystem

    service.create_text(0, "new.txt", "hello")
    assert (root / "new.txt").read_text(encoding="utf-8") == "hello"

    with pytest.raises(FileExistsError):
        service.create_text(0, "new.txt", "overwrite")


def test_rejects_unsafe_filename(filesystem):
    service, _ = filesystem

    with pytest.raises(ValueError):
        service.read_text(0, "../outside.txt")


def test_rejects_hidden_file(filesystem):
    service, _ = filesystem

    with pytest.raises(ValueError):
        service.create_text(0, ".secret", "private")


def test_rejects_invalid_directory_index(filesystem):
    service, _ = filesystem

    with pytest.raises(ValueError):
        service.list_files(99)


def test_rejects_oversized_file(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (allowed / "large.txt").write_text("123456", encoding="utf-8")
    service = FilesystemService((allowed,), max_read_bytes=5)

    with pytest.raises(ValueError, match="size"):
        service.read_text(0, "large.txt")



def test_rejects_protected_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(
        FilesystemService,
        "_protected_locations",
        staticmethod(lambda: (tmp_path.resolve(),)),
    )

    with pytest.raises(ValueError, match="System directories"):
        FilesystemService((tmp_path,))



def test_deletes_file(filesystem):
    service, root = filesystem
    (root / "old.txt").write_text("temporary", encoding="utf-8")

    assert service.delete_file(0, "old.txt") == "Deleted old.txt"
    assert not (root / "old.txt").exists()
