import os
from pathlib import Path

import pytest

from codex_autorunner.core import filebox


def _write(dir_path: Path, name: str, content: bytes = b"x") -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    path = dir_path / name
    path.write_bytes(content)
    return path


def test_list_filebox_only_returns_canonical_entries(tmp_path: Path) -> None:
    repo = tmp_path
    _write(filebox.inbox_dir(repo), "primary.txt", b"primary")
    _write(repo / ".codex-autorunner" / "pma" / "inbox", "legacy.txt", b"legacy")

    listing = filebox.list_filebox(repo)
    assert [entry.name for entry in listing["inbox"]] == ["primary.txt"]
    assert listing["outbox"] == []


def test_resolve_ignores_legacy_paths(tmp_path: Path) -> None:
    repo = tmp_path
    _write(repo / ".codex-autorunner" / "pma" / "inbox", "shared.txt", b"legacy")
    assert filebox.resolve_file(repo, "inbox", "shared.txt") is None


def test_save_resolve_and_delete(tmp_path: Path) -> None:
    repo = tmp_path
    filebox.save_file(repo, "inbox", "note.md", b"hello")
    entry = filebox.resolve_file(repo, "inbox", "note.md")
    assert entry is not None
    assert entry.source == "filebox"
    assert entry.path.read_bytes() == b"hello"

    removed = filebox.delete_file(repo, "inbox", "note.md")
    assert removed
    assert filebox.resolve_file(repo, "inbox", "note.md") is None


def test_list_regular_files_sorts_newest_first(tmp_path: Path) -> None:
    folder = tmp_path / "files"
    older = _write(folder, "older.txt", b"old")
    newer = _write(folder, "newer.txt", b"new")
    (folder / "nested").mkdir()

    os.utime(older, (1, 1))
    os.utime(newer, (2, 2))

    assert [path.name for path in filebox.list_regular_files(folder)] == [
        "newer.txt",
        "older.txt",
    ]


def test_delete_regular_files_only_removes_regular_files(tmp_path: Path) -> None:
    folder = tmp_path / "files"
    first = _write(folder, "first.txt", b"1")
    second = _write(folder, "second.txt", b"2")
    (folder / "nested").mkdir()

    deleted = filebox.delete_regular_files(folder)

    assert deleted == 2
    assert not first.exists()
    assert not second.exists()
    assert (folder / "nested").exists()


def test_delete_ignores_legacy_duplicates(tmp_path: Path) -> None:
    repo = tmp_path
    _write(repo / ".codex-autorunner" / "pma" / "inbox", "shared.txt", b"legacy")

    removed = filebox.delete_file(repo, "inbox", "shared.txt")
    assert not removed
    assert (repo / ".codex-autorunner" / "pma" / "inbox" / "shared.txt").exists()


@pytest.mark.parametrize(
    "name",
    [
        "../secret.txt",
        "subdir/file.txt",
        "trailing/",
        "/absolute.txt",
        "..",
        ".",
    ],
)
def test_save_rejects_invalid_names(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError):
        filebox.save_file(tmp_path, "inbox", name, b"x")
