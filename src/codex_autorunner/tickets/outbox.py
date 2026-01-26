from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .frontmatter import parse_markdown_frontmatter
from .lint import lint_dispatch_frontmatter
from .models import Dispatch, DispatchRecord


@dataclass(frozen=True)
class OutboxPaths:
    """Filesystem paths for the dispatch outbox."""

    run_dir: Path
    dispatch_dir: Path
    dispatch_history_dir: Path
    dispatch_path: Path


def resolve_outbox_paths(
    *, workspace_root: Path, runs_dir: Path, run_id: str
) -> OutboxPaths:
    run_dir = workspace_root / runs_dir / run_id
    dispatch_dir = run_dir / "dispatch"
    dispatch_history_dir = run_dir / "dispatch_history"
    dispatch_path = run_dir / "DISPATCH.md"
    return OutboxPaths(
        run_dir=run_dir,
        dispatch_dir=dispatch_dir,
        dispatch_history_dir=dispatch_history_dir,
        dispatch_path=dispatch_path,
    )


def ensure_outbox_dirs(paths: OutboxPaths) -> None:
    paths.dispatch_dir.mkdir(parents=True, exist_ok=True)
    paths.dispatch_history_dir.mkdir(parents=True, exist_ok=True)


def _copy_item(src: Path, dst: Path) -> None:
    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def _list_dispatch_items(dispatch_dir: Path) -> list[Path]:
    if not dispatch_dir.exists() or not dispatch_dir.is_dir():
        return []
    items: list[Path] = []
    for child in sorted(dispatch_dir.iterdir(), key=lambda p: p.name):
        if child.name.startswith("."):
            continue
        items.append(child)
    return items


def _delete_dispatch_items(items: list[Path]) -> None:
    for item in items:
        try:
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
        except OSError:
            # Best-effort cleanup.
            continue


def parse_dispatch(path: Path) -> tuple[Optional[Dispatch], list[str]]:
    """Parse a dispatch file (DISPATCH.md) into a Dispatch object."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, [f"Failed to read dispatch file: {exc}"]

    data, body = parse_markdown_frontmatter(raw)
    normalized, errors = lint_dispatch_frontmatter(data)
    if errors:
        return None, errors

    mode = normalized.get("mode", "notify")
    title = normalized.get("title")
    title_str = title.strip() if isinstance(title, str) and title.strip() else None
    extra = dict(normalized)
    extra.pop("mode", None)
    extra.pop("title", None)
    return (
        Dispatch(mode=mode, body=body.lstrip("\n"), title=title_str, extra=extra),
        [],
    )


def archive_dispatch(
    paths: OutboxPaths, *, next_seq: int
) -> tuple[Optional[DispatchRecord], list[str]]:
    """Archive the current dispatch and attachments to the dispatch history.

    Moves DISPATCH.md + attachments into dispatch_history/<seq>/.

    Returns (DispatchRecord, []) on success.
    Returns (None, []) when no dispatch file exists.
    Returns (None, errors) on failure.
    """

    if not paths.dispatch_path.exists():
        return None, []

    dispatch, errors = parse_dispatch(paths.dispatch_path)
    if errors or dispatch is None:
        return None, errors

    items = _list_dispatch_items(paths.dispatch_dir)
    dest = paths.dispatch_history_dir / f"{next_seq:04d}"
    try:
        dest.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        return None, [f"Failed to create dispatch history dir: {exc}"]

    archived: list[Path] = []
    try:
        # Archive the dispatch file.
        msg_dest = dest / "DISPATCH.md"
        _copy_item(paths.dispatch_path, msg_dest)
        archived.append(msg_dest)

        # Archive all attachments.
        for item in items:
            item_dest = dest / item.name
            _copy_item(item, item_dest)
            archived.append(item_dest)

    except OSError as exc:
        return None, [f"Failed to archive dispatch: {exc}"]

    # Cleanup (best-effort).
    try:
        paths.dispatch_path.unlink()
    except OSError:
        pass
    _delete_dispatch_items(items)

    return (
        DispatchRecord(
            seq=next_seq,
            dispatch=dispatch,
            archived_dir=dest,
            archived_files=tuple(archived),
        ),
        [],
    )
