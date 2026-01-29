from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .frontmatter import parse_markdown_frontmatter


@dataclass(frozen=True)
class ReplyPaths:
    run_dir: Path
    reply_dir: Path
    reply_history_dir: Path
    user_reply_path: Path


@dataclass(frozen=True)
class UserReply:
    body: str
    title: Optional[str] = None
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ReplyDispatch:
    seq: int
    reply: UserReply
    archived_dir: Path
    archived_files: tuple[Path, ...]


def resolve_reply_paths(
    *, workspace_root: Path, runs_dir: Path, run_id: str
) -> ReplyPaths:
    run_dir = workspace_root / runs_dir / run_id
    reply_dir = run_dir / "reply"
    reply_history_dir = run_dir / "reply_history"
    user_reply_path = run_dir / "USER_REPLY.md"
    return ReplyPaths(
        run_dir=run_dir,
        reply_dir=reply_dir,
        reply_history_dir=reply_history_dir,
        user_reply_path=user_reply_path,
    )


def ensure_reply_dirs(paths: ReplyPaths) -> None:
    paths.reply_dir.mkdir(parents=True, exist_ok=True)
    paths.reply_history_dir.mkdir(parents=True, exist_ok=True)


def parse_user_reply(path: Path) -> tuple[Optional[UserReply], list[str]]:
    """Parse a USER_REPLY.md file.

    USER_REPLY.md is intentionally permissive:
    - frontmatter is optional
    - we accept any YAML keys (stored in `extra`)
    """

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, [f"Failed to read USER_REPLY.md: {exc}"]

    data, body = parse_markdown_frontmatter(raw)
    title = data.get("title")
    title_str = title.strip() if isinstance(title, str) and title.strip() else None
    extra = dict(data)
    extra.pop("title", None)

    # Keep the body as-is, but normalize leading whitespace so it mirrors DISPATCH.md.
    return UserReply(body=body.lstrip("\n"), title=title_str, extra=extra), []


def _copy_item(src: Path, dst: Path) -> None:
    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def _list_reply_items(reply_dir: Path) -> list[Path]:
    if not reply_dir.exists() or not reply_dir.is_dir():
        return []
    items: list[Path] = []
    for child in sorted(reply_dir.iterdir(), key=lambda p: p.name):
        if child.name.startswith("."):
            continue
        items.append(child)
    return items


def _delete_items(items: list[Path]) -> None:
    for item in items:
        try:
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
        except OSError:
            continue


_SEQ_RE = re.compile(r"^[0-9]{4}$")


def next_reply_seq(reply_history_dir: Path) -> int:
    """Return the next sequence number for reply_history."""

    if not reply_history_dir.exists() or not reply_history_dir.is_dir():
        return 1
    existing: list[int] = []
    for child in reply_history_dir.iterdir():
        try:
            if not child.is_dir():
                continue
            if not _SEQ_RE.fullmatch(child.name):
                continue
            existing.append(int(child.name))
        except OSError:
            continue
    return (max(existing) + 1) if existing else 1


def dispatch_reply(
    paths: ReplyPaths, *, next_seq: int
) -> tuple[Optional[ReplyDispatch], list[str]]:
    """Archive USER_REPLY.md + reply/* into reply_history/<seq>/.

    Returns (dispatch, errors). When USER_REPLY.md does not exist, returns (None, []).
    """

    if not paths.user_reply_path.exists():
        return None, []

    reply, errors = parse_user_reply(paths.user_reply_path)
    if errors or reply is None:
        return None, errors

    items = _list_reply_items(paths.reply_dir)
    dest = paths.reply_history_dir / f"{next_seq:04d}"
    try:
        dest.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        return None, [f"Failed to create reply history dir: {exc}"]

    archived: list[Path] = []
    try:
        msg_dest = dest / "USER_REPLY.md"
        _copy_item(paths.user_reply_path, msg_dest)
        archived.append(msg_dest)

        for item in items:
            item_dest = dest / item.name
            _copy_item(item, item_dest)
            archived.append(item_dest)
    except OSError as exc:
        return None, [f"Failed to archive reply: {exc}"]

    # Cleanup (best-effort).
    try:
        paths.user_reply_path.unlink()
    except OSError:
        pass
    _delete_items(items)

    return (
        ReplyDispatch(
            seq=next_seq,
            reply=reply,
            archived_dir=dest,
            archived_files=tuple(archived),
        ),
        [],
    )
