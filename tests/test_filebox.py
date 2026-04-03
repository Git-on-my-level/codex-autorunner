import os
from pathlib import Path

import pytest

from codex_autorunner.core import filebox, filebox_lifecycle


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


def test_consume_inbox_file_moves_file_out_of_active_inbox(tmp_path: Path) -> None:
    repo = tmp_path
    filebox.save_file(repo, "inbox", "note.md", b"hello")

    archived = filebox_lifecycle.consume_inbox_file(repo, "note.md")

    assert archived.box == "consumed"
    assert archived.path.read_bytes() == b"hello"
    assert filebox.resolve_file(repo, "inbox", "note.md") is None
    archived_entries = filebox_lifecycle.list_consumed_files(repo)
    assert [(entry.box, entry.name) for entry in archived_entries] == [
        ("consumed", "note.md")
    ]


def test_dismiss_and_restore_file_preserve_contents(tmp_path: Path) -> None:
    repo = tmp_path
    filebox.save_file(repo, "inbox", "skip.md", b"skip")

    dismissed = filebox_lifecycle.dismiss_inbox_file(repo, "skip.md")
    assert filebox.resolve_file(repo, "inbox", "skip.md") is None
    restored = filebox_lifecycle.unconsume_inbox_file(repo, "skip.md")

    assert dismissed.box == "dismissed"
    assert restored.box == "inbox"
    assert restored.path.read_bytes() == b"skip"
    assert not (filebox_lifecycle.dismissed_dir(repo) / "skip.md").exists()
    inbox_entry = filebox.resolve_file(repo, "inbox", "skip.md")
    assert inbox_entry is not None
    assert inbox_entry.path.read_bytes() == b"skip"


def test_consume_inbox_file_suffixes_archive_name_on_collision(tmp_path: Path) -> None:
    repo = tmp_path
    _write(filebox_lifecycle.consumed_dir(repo), "note.md", b"first")
    filebox.save_file(repo, "inbox", "note.md", b"second")

    archived = filebox_lifecycle.consume_inbox_file(repo, "note.md")

    assert archived.box == "consumed"
    assert archived.name == "note-2.md"
    assert archived.path.read_bytes() == b"second"
    assert (filebox_lifecycle.consumed_dir(repo) / "note.md").read_bytes() == b"first"
    assert filebox.resolve_file(repo, "inbox", "note.md") is None


def test_restore_chooses_newest_archive_when_same_name_exists_in_both_boxes(
    tmp_path: Path,
) -> None:
    repo = tmp_path
    consumed = _write(filebox_lifecycle.consumed_dir(repo), "brief.md", b"consumed")
    dismissed = _write(filebox_lifecycle.dismissed_dir(repo), "brief.md", b"dismissed")
    os.utime(consumed, (1, 1))
    os.utime(dismissed, (2, 2))

    restored = filebox_lifecycle.unconsume_inbox_file(repo, "brief.md")

    assert restored.box == "inbox"
    assert restored.path.read_bytes() == b"dismissed"
    assert consumed.exists()
    assert not dismissed.exists()


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
