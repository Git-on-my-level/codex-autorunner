from pathlib import Path

import pytest

from codex_autorunner.core import filebox


def _write(dir_path: Path, name: str, content: bytes = b"x") -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    path = dir_path / name
    path.write_bytes(content)
    return path


def test_migrate_legacy_copies_and_lists(tmp_path: Path) -> None:
    repo = tmp_path
    # Legacy PMA inbox/outbox
    _write(repo / ".codex-autorunner" / "pma" / "inbox", "pma.txt", b"pma")
    _write(repo / ".codex-autorunner" / "pma" / "outbox", "pma-out.txt", b"out")
    # Legacy Telegram inbox/outbox
    topic = repo / ".codex-autorunner" / "uploads" / "telegram-files" / "topic-1"
    _write(topic / "inbox", "tg.txt", b"tg")
    _write(topic / "outbox" / "pending", "tg-out.txt", b"tgout")

    copied = filebox.migrate_legacy(repo)
    # 4 unique files should copy across inbox/outbox
    assert copied == 4

    listing = filebox.list_filebox(repo)
    inbox_names = {e.name for e in listing["inbox"]}
    outbox_names = {e.name for e in listing["outbox"]}
    assert {"pma.txt", "tg.txt"} <= inbox_names
    assert {"pma-out.txt", "tg-out.txt"} <= outbox_names


def test_filebox_dedupes_over_legacy(tmp_path: Path) -> None:
    repo = tmp_path
    # Both legacy and primary have same filename; primary should win.
    _write(repo / ".codex-autorunner" / "pma" / "inbox", "shared.txt", b"legacy")
    _write(filebox.inbox_dir(repo), "shared.txt", b"primary")

    listing = filebox.list_filebox(repo)
    entry = next(e for e in listing["inbox"] if e.name == "shared.txt")
    assert entry.source == "filebox"
    assert entry.path.read_bytes() == b"primary"


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
