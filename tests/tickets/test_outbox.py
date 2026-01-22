from __future__ import annotations

from pathlib import Path

from codex_autorunner.tickets.outbox import (
    dispatch_outbox,
    ensure_outbox_dirs,
    parse_user_message,
    resolve_outbox_paths,
)


def _write_user_message(path: Path, *, mode: str = "notify", body: str = "Hello", title: str | None = None) -> None:
    title_line = f"title: {title}\n" if title else ""
    content = f"---\nmode: {mode}\n{title_line}---\n\n{body}\n"
    path.write_text(content, encoding="utf-8")


def test_dispatch_outbox_no_user_message_is_noop(tmp_path: Path) -> None:
    paths = resolve_outbox_paths(
        workspace_root=tmp_path,
        runs_dir=Path(".codex-autorunner/runs"),
        run_id="run-1",
    )
    ensure_outbox_dirs(paths)

    dispatch, errors = dispatch_outbox(paths, next_seq=1)
    assert dispatch is None
    assert errors == []


def test_dispatch_outbox_archives_message_and_attachments(tmp_path: Path) -> None:
    paths = resolve_outbox_paths(
        workspace_root=tmp_path,
        runs_dir=Path(".codex-autorunner/runs"),
        run_id="run-1",
    )
    ensure_outbox_dirs(paths)

    # Attachment first.
    (paths.handoff_dir / "review.md").write_text("Please review", encoding="utf-8")
    _write_user_message(paths.user_message_path, mode="pause", body="Review attached")

    dispatch, errors = dispatch_outbox(paths, next_seq=1)
    assert errors == []
    assert dispatch is not None
    assert dispatch.seq == 1
    assert dispatch.message.mode == "pause"
    assert dispatch.archived_dir.exists()
    assert (dispatch.archived_dir / "USER_MESSAGE.md").exists()
    assert (dispatch.archived_dir / "review.md").exists()

    # Outbox cleared after dispatch.
    assert not paths.user_message_path.exists()
    assert list(paths.handoff_dir.iterdir()) == []

    # Subsequent dispatch is a noop.
    dispatch2, errors2 = dispatch_outbox(paths, next_seq=2)
    assert dispatch2 is None
    assert errors2 == []


def test_dispatch_outbox_invalid_user_message_frontmatter_does_not_delete(tmp_path: Path) -> None:
    paths = resolve_outbox_paths(
        workspace_root=tmp_path,
        runs_dir=Path(".codex-autorunner/runs"),
        run_id="run-1",
    )
    ensure_outbox_dirs(paths)

    _write_user_message(paths.user_message_path, mode="bad", body="x")
    dispatch, errors = dispatch_outbox(paths, next_seq=1)
    assert dispatch is None
    assert errors

    # File should remain for manual/agent correction.
    assert paths.user_message_path.exists()

    message, parse_errors = parse_user_message(paths.user_message_path)
    assert message is None
    assert parse_errors
