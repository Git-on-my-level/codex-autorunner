import os
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.datastructures import UploadFile

from codex_autorunner.core import filebox, filebox_lifecycle
from codex_autorunner.surfaces.web.routes.filebox import (
    _read_upload_limited,
    _upload_files_to_box,
)


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
    loaded = filebox.read_file(repo, "inbox", "note.md")
    assert loaded is not None
    loaded_entry, loaded_data = loaded
    assert loaded_entry.name == "note.md"
    assert loaded_data == b"hello"
    opened = filebox.open_file(repo, "inbox", "note.md")
    assert opened is not None
    opened_entry, opened_handle = opened
    with opened_handle:
        assert opened_entry.name == "note.md"
        assert opened_handle.read() == b"hello"

    removed = filebox.delete_file(repo, "inbox", "note.md")
    assert removed
    assert filebox.resolve_file(repo, "inbox", "note.md") is None


def test_resolve_ignores_symlinked_filebox_entries(tmp_path: Path) -> None:
    repo = tmp_path
    outside = _write(tmp_path / "outside", "secret.txt", b"secret")
    inbox = filebox.inbox_dir(repo)
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "link.txt").symlink_to(outside)

    assert filebox.resolve_file(repo, "inbox", "link.txt") is None
    assert filebox.list_filebox(repo)["inbox"] == []


def test_filebox_ignores_hardlinked_entries(tmp_path: Path) -> None:
    repo = tmp_path
    outside = _write(tmp_path / "outside", "secret.txt", b"secret")
    inbox = filebox.inbox_dir(repo)
    inbox.mkdir(parents=True, exist_ok=True)
    try:
        os.link(outside, inbox / "hardlink.txt")
    except OSError as exc:
        pytest.skip(f"hard links unavailable: {exc}")

    assert filebox.resolve_file(repo, "inbox", "hardlink.txt") is None
    assert filebox.read_file(repo, "inbox", "hardlink.txt") is None
    assert filebox.open_file(repo, "inbox", "hardlink.txt") is None
    assert filebox.list_filebox(repo)["inbox"] == []


def test_save_rejects_symlinked_filebox_target(tmp_path: Path) -> None:
    repo = tmp_path
    target = _write(tmp_path / "outside", "secret.txt", b"secret")
    inbox = filebox.inbox_dir(repo)
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "note.md").symlink_to(target)

    with pytest.raises(ValueError):
        filebox.save_file(repo, "inbox", "note.md", b"replacement")
    assert target.read_bytes() == b"secret"


def test_save_rejects_hardlinked_filebox_target(tmp_path: Path) -> None:
    repo = tmp_path
    target = _write(tmp_path / "outside", "secret.txt", b"secret")
    inbox = filebox.inbox_dir(repo)
    inbox.mkdir(parents=True, exist_ok=True)
    try:
        os.link(target, inbox / "note.md")
    except OSError as exc:
        pytest.skip(f"hard links unavailable: {exc}")

    with pytest.raises(ValueError):
        filebox.save_file(repo, "inbox", "note.md", b"replacement")
    assert target.read_bytes() == b"secret"


def test_save_rejects_symlinked_filebox_box_dir(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    outside = tmp_path / "outside-inbox"
    outside.mkdir()
    inbox = filebox.inbox_dir(repo)
    inbox.parent.mkdir(parents=True, exist_ok=True)
    inbox.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="must not be a symlink"):
        filebox.save_file(repo, "inbox", "note.md", b"secret")
    assert not (outside / "note.md").exists()


def test_save_rejects_symlinked_filebox_root(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    outside = tmp_path / "outside-filebox"
    outside.mkdir()
    root = filebox.filebox_root(repo)
    root.parent.mkdir(parents=True, exist_ok=True)
    root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="must not be a symlink"):
        filebox.save_file(repo, "inbox", "note.md", b"secret")
    assert not (outside / "inbox" / "note.md").exists()


def test_save_rejects_symlinked_repo_root(tmp_path: Path) -> None:
    real_repo = tmp_path / "real-repo"
    real_repo.mkdir()
    repo = tmp_path / "repo-link"
    repo.symlink_to(real_repo, target_is_directory=True)

    with pytest.raises(ValueError, match="must not be a symlink"):
        filebox.save_file(repo, "inbox", "note.md", b"secret")
    assert not (filebox.inbox_dir(real_repo) / "note.md").exists()


def test_list_filebox_rejects_symlinked_control_root(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    outside = tmp_path / "outside-control"
    outside.mkdir()
    control_root = repo / ".codex-autorunner"
    control_root.parent.mkdir(parents=True, exist_ok=True)
    control_root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="must not be a symlink"):
        filebox.list_filebox(repo)


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


def test_list_regular_files_skips_symlinked_files(tmp_path: Path) -> None:
    folder = tmp_path / "files"
    real = _write(folder, "real.txt", b"real")
    outside = _write(tmp_path / "outside", "secret.txt", b"secret")
    (folder / "link.txt").symlink_to(outside)

    assert filebox.list_regular_files(folder) == [real]


def test_lifecycle_rejects_symlinked_archive_entries(tmp_path: Path) -> None:
    repo = tmp_path
    filebox.save_file(repo, "inbox", "note.md", b"hello")
    consumed = filebox_lifecycle.consumed_dir(repo)
    consumed.mkdir(parents=True, exist_ok=True)
    link = consumed / "note.md"
    link.symlink_to(filebox.inbox_dir(repo) / "note.md")

    with pytest.raises(ValueError, match="Invalid filename"):
        filebox_lifecycle.consume_inbox_file(repo, "note.md")


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


class _ChunkedUpload:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    async def read(self, _size: int) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


@pytest.mark.asyncio
async def test_upload_reader_enforces_max_bytes() -> None:
    upload = _ChunkedUpload([b"abc", b"def"])

    with pytest.raises(ValueError, match="File too large"):
        await _read_upload_limited(upload, max_upload_bytes=5)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_filebox_upload_limit_failure_does_not_partially_save(
    tmp_path: Path,
) -> None:
    class _Request:
        app = SimpleNamespace(
            state=SimpleNamespace(
                config=SimpleNamespace(pma=SimpleNamespace(max_upload_bytes=3))
            )
        )

        async def form(self):
            return {
                "small": UploadFile(filename="small.txt", file=BytesIO(b"ok")),
                "large": UploadFile(filename="large.txt", file=BytesIO(b"too-big")),
            }

    with pytest.raises(Exception, match="File too large"):
        await _upload_files_to_box(
            repo_root=tmp_path,
            box="inbox",
            request=_Request(),  # type: ignore[arg-type]
        )

    assert filebox.list_filebox(tmp_path)["inbox"] == []


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
