from pathlib import Path

import pytest

from app.security.command_validator import resolve_safe_path


def test_resolves_plain_filename_inside_base(tmp_path):
    result = resolve_safe_path(tmp_path, "notes.txt")

    assert result == (tmp_path / "notes.txt").resolve()
    assert result.parent == tmp_path.resolve()


@pytest.mark.parametrize(
    "name",
    [
        "",
        ".secret",
        "..",
        "../outside.txt",
        "folder/file.txt",
        r"folder\file.txt",
        "/etc/passwd",
        "bad\x00name.txt",
        "bad\nname.txt",
        "C:secret.txt",
        "C:\\secret.txt",
        r"\\server\share\secret.txt",
        "notes.txt:payload",
        "bad?.txt",
        "bad*.txt",
        "bad|name.txt",
        "bad<name.txt",
        "bad>name.txt",
        "trailing.",
        "trailing ",
        "CON",
        "CON.txt",
        "nul",
        "AUX.log",
        "COM1.txt",
        "LPT9.csv",
    ],
)
def test_rejects_unsafe_filenames(tmp_path, name):
    with pytest.raises(ValueError):
        resolve_safe_path(tmp_path, name)


def test_rejects_filename_longer_than_limit(tmp_path):
    with pytest.raises(ValueError, match="too long"):
        resolve_safe_path(tmp_path, "a" * 256)


@pytest.mark.parametrize(
    "name",
    [None, 123, 1.5, Path("notes.txt")],
)
def test_rejects_non_string_filenames(tmp_path, name):
    with pytest.raises(ValueError, match="Filename"):
        resolve_safe_path(tmp_path, name)


@pytest.mark.parametrize(
    "name",
    [
        "notes.txt",
        "report.final.txt",
        "my-notes_2 (draft).txt",
    ],
)

def test_accepts_ordinary_filenames(tmp_path, name):
    result = resolve_safe_path(tmp_path, name)

    assert result.parent == tmp_path.resolve()
    assert result.name == name

def test_rejects_symlink_that_escapes_base(tmp_path):
    base = tmp_path / "safe"
    outside = tmp_path / "outside"

    base.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")

    link = base / "escape.txt"

    try:
        link.symlink_to(outside / "secret.txt")
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation is not supported in this environment")

    with pytest.raises(ValueError, match="escapes"):
        resolve_safe_path(base, "escape.txt")