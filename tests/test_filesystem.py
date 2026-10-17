
import pytest

from app.commands.filesystem import FilesystemService


@pytest.fixture
def filesystem(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()

    service = FilesystemService((allowed,))
    try:
        yield service, allowed
    finally:
        service.close()


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

    with FilesystemService((allowed,), max_read_bytes=5) as service:
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
def test_rejects_symlink_when_reading(filesystem, tmp_path):
    service, root = filesystem
    outside = tmp_path / "outside.txt"
    outside.write_text("private data", encoding="utf-8")
    link = root / "linked.txt"
    link.symlink_to(outside)

    with pytest.raises((ValueError, OSError)):
        service.read_text(0, "linked.txt")


def test_rejects_symlink_when_deleting(filesystem, tmp_path):
    service, root = filesystem
    outside = tmp_path / "outside.txt"
    outside.write_text("keep this", encoding="utf-8")
    link = root / "linked.txt"
    link.symlink_to(outside)

    with pytest.raises((ValueError, OSError)):
        service.delete_file(0, "linked.txt")

    assert outside.read_text(encoding="utf-8") == "keep this"
    assert link.is_symlink()


def test_rejects_boolean_directory_index(filesystem):
    service, _ = filesystem

    with pytest.raises(ValueError, match="index"):
        service.read_text(True, "notes.txt")
def test_close_is_idempotent(filesystem):
    service, _ = filesystem

    service.close()
    service.close()


def test_closed_service_rejects_operations(filesystem):
    service, _ = filesystem
    service.close()

    with pytest.raises(RuntimeError, match="closed"):
        service.list_files()

    with pytest.raises(RuntimeError, match="closed"):
        service.read_text(0, "notes.txt")

    with pytest.raises(RuntimeError, match="closed"):
        service.create_text(0, "new.txt", "content")

    with pytest.raises(RuntimeError, match="closed"):
        service.delete_file(0, "notes.txt")

    with pytest.raises(RuntimeError, match="closed"):
        service.directory_count


def test_closed_service_rejects_context_entry(filesystem):
    service, _ = filesystem
    service.close()

    with pytest.raises(RuntimeError, match="closed"):
        service.__enter__()
