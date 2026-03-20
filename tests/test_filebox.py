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
