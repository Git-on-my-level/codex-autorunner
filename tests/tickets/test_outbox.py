from __future__ import annotations

from pathlib import Path

from codex_autorunner.tickets.outbox import (
    archive_dispatch,
    ensure_outbox_dirs,
    parse_dispatch,
    resolve_outbox_paths,
)


def _write_dispatch(
    path: Path, *, mode: str = "notify", body: str = "Hello", title: str | None = None
) -> None:
    """Write a dispatch file (DISPATCH.md) with the given content."""
    title_line = f"title: {title}\n" if title else ""
    content = f"---\nmode: {mode}\n{title_line}---\n\n{body}\n"
    path.write_text(content, encoding="utf-8")


def test_archive_dispatch_no_dispatch_file_is_noop(tmp_path: Path) -> None:
    """When no dispatch file exists, archive_dispatch returns (None, [])."""
    paths = resolve_outbox_paths(
        workspace_root=tmp_path,
        runs_dir=Path(".codex-autorunner/runs"),
        run_id="run-1",
    )
    ensure_outbox_dirs(paths)

    record, errors = archive_dispatch(paths, next_seq=1)
    assert record is None
    assert errors == []


def test_archive_dispatch_archives_dispatch_and_attachments(tmp_path: Path) -> None:
    """Archiving moves dispatch file and attachments to dispatch history."""
    paths = resolve_outbox_paths(
        workspace_root=tmp_path,
        runs_dir=Path(".codex-autorunner/runs"),
        run_id="run-1",
    )
    ensure_outbox_dirs(paths)

    # Attachment first.
    (paths.dispatch_dir / "review.md").write_text("Please review", encoding="utf-8")
    _write_dispatch(paths.dispatch_path, mode="pause", body="Review attached")

    record, errors = archive_dispatch(paths, next_seq=1)
    assert errors == []
    assert record is not None
    assert record.seq == 1
    assert record.dispatch.mode == "pause"
    assert record.dispatch.is_handoff is True  # pause mode = handoff
    assert record.archived_dir.exists()
    assert (record.archived_dir / "DISPATCH.md").exists()
    assert (record.archived_dir / "review.md").exists()

    # Outbox cleared after archiving.
    assert not paths.dispatch_path.exists()
    assert list(paths.dispatch_dir.iterdir()) == []

    # Subsequent archive is a noop.
    record2, errors2 = archive_dispatch(paths, next_seq=2)
    assert record2 is None
    assert errors2 == []


def test_archive_dispatch_invalid_frontmatter_does_not_delete(
    tmp_path: Path,
) -> None:
    """Invalid dispatch frontmatter returns errors but doesn't delete file."""
    paths = resolve_outbox_paths(
        workspace_root=tmp_path,
        runs_dir=Path(".codex-autorunner/runs"),
        run_id="run-1",
    )
    ensure_outbox_dirs(paths)

    _write_dispatch(paths.dispatch_path, mode="bad", body="x")
    record, errors = archive_dispatch(paths, next_seq=1)
    assert record is None
    assert errors

    # File should remain for manual/agent correction.
    assert paths.dispatch_path.exists()

    dispatch, parse_errors = parse_dispatch(paths.dispatch_path)
    assert dispatch is None
    assert parse_errors
