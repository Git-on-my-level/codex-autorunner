from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .frontmatter import parse_markdown_frontmatter
from .lint import lint_user_message_frontmatter
from .models import OutboxDispatch, UserMessage


@dataclass(frozen=True)
class OutboxPaths:
    run_dir: Path
    handoff_dir: Path
    handoff_history_dir: Path
    user_message_path: Path


def resolve_outbox_paths(*, workspace_root: Path, runs_dir: Path, run_id: str) -> OutboxPaths:
    run_dir = workspace_root / runs_dir / run_id
    handoff_dir = run_dir / "handoff"
    handoff_history_dir = run_dir / "handoff_history"
    user_message_path = run_dir / "USER_MESSAGE.md"
    return OutboxPaths(
        run_dir=run_dir,
        handoff_dir=handoff_dir,
        handoff_history_dir=handoff_history_dir,
        user_message_path=user_message_path,
    )


def ensure_outbox_dirs(paths: OutboxPaths) -> None:
    paths.handoff_dir.mkdir(parents=True, exist_ok=True)
    paths.handoff_history_dir.mkdir(parents=True, exist_ok=True)


def _copy_item(src: Path, dst: Path) -> None:
    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def _list_handoff_items(handoff_dir: Path) -> list[Path]:
    if not handoff_dir.exists() or not handoff_dir.is_dir():
        return []
    items: list[Path] = []
    for child in sorted(handoff_dir.iterdir(), key=lambda p: p.name):
        if child.name.startswith("."):
            continue
        items.append(child)
    return items


def _delete_handoff_items(items: list[Path]) -> None:
    for item in items:
        try:
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
        except OSError:
            # Best-effort cleanup.
            continue


def parse_user_message(path: Path) -> tuple[Optional[UserMessage], list[str]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, [f"Failed to read USER_MESSAGE.md: {exc}"]

    data, body = parse_markdown_frontmatter(raw)
    normalized, errors = lint_user_message_frontmatter(data)
    if errors:
        return None, errors

    mode = normalized.get("mode", "notify")
    title = normalized.get("title")
    title_str = title.strip() if isinstance(title, str) and title.strip() else None
    extra = dict(normalized)
    extra.pop("mode", None)
    extra.pop("title", None)
    return UserMessage(mode=mode, body=body.lstrip("\n"), title=title_str, extra=extra), []


def dispatch_outbox(paths: OutboxPaths, *, next_seq: int) -> tuple[Optional[OutboxDispatch], list[str]]:
    """Archive USER_MESSAGE.md + handoff/* into handoff_history/<seq>/.

    Returns (dispatch, errors). When USER_MESSAGE.md does not exist, returns (None, []).
    """

    if not paths.user_message_path.exists():
        return None, []

    message, errors = parse_user_message(paths.user_message_path)
    if errors or message is None:
        return None, errors

    items = _list_handoff_items(paths.handoff_dir)
    dest = paths.handoff_history_dir / f"{next_seq:04d}"
    try:
        dest.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        return None, [f"Failed to create handoff history dir: {exc}"]

    archived: list[Path] = []
    try:
        # Archive user message.
        msg_dest = dest / "USER_MESSAGE.md"
        _copy_item(paths.user_message_path, msg_dest)
        archived.append(msg_dest)

        # Archive all handoff items.
        for item in items:
            item_dest = dest / item.name
            _copy_item(item, item_dest)
            archived.append(item_dest)

    except OSError as exc:
        return None, [f"Failed to archive outbox: {exc}"]

    # Cleanup (best-effort).
    try:
        paths.user_message_path.unlink()
    except OSError:
        pass
    _delete_handoff_items(items)

    return (
        OutboxDispatch(
            seq=next_seq,
            message=message,
            archived_dir=dest,
            archived_files=tuple(archived),
        ),
        [],
    )
