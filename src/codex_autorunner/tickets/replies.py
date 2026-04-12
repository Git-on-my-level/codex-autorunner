from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from codex_autorunner.core.filesystem import copy_path

from .files import safe_relpath
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


def resolve_reply_paths(*, workspace_root: Path, run_id: str) -> ReplyPaths:
    run_dir = workspace_root / ".codex-autorunner" / "runs" / run_id
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
        copy_path(paths.user_reply_path, msg_dest)
        archived.append(msg_dest)

        for item in items:
            item_dest = dest / item.name
            copy_path(item, item_dest)
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


def render_reply_context(
    *,
    reply_history_dir: Path,
    last_seq: int,
    workspace_root: Path,
) -> tuple[str, int]:
    """Render new human replies (reply_history) into a prompt block."""

    if not reply_history_dir.exists() or not reply_history_dir.is_dir():
        return "", last_seq

    entries: list[tuple[int, Path]] = []
    try:
        for child in reply_history_dir.iterdir():
            try:
                if not child.is_dir():
                    continue
                name = child.name
                if not (len(name) == 4 and name.isdigit()):
                    continue
                seq = int(name)
                if seq <= last_seq:
                    continue
                entries.append((seq, child))
            except OSError:
                continue
    except OSError:
        return "", last_seq

    if not entries:
        return "", last_seq

    entries.sort(key=lambda x: x[0])
    max_seq = max(seq for seq, _ in entries)

    blocks: list[str] = []
    for seq, entry_dir in entries:
        reply_path = entry_dir / "USER_REPLY.md"
        reply, errors = (
            parse_user_reply(reply_path)
            if reply_path.exists()
            else (None, ["USER_REPLY.md missing"])
        )

        block_lines: list[str] = [f"[USER_REPLY {seq:04d}]"]
        if errors:
            block_lines.append("Errors:\n- " + "\n- ".join(errors))
        if reply is not None:
            if reply.title:
                block_lines.append(f"Title: {reply.title}")
            if reply.body:
                block_lines.append(reply.body)

        attachments: list[str] = []
        try:
            for child in sorted(entry_dir.iterdir(), key=lambda p: p.name):
                try:
                    if child.name.startswith("."):
                        continue
                    if child.name == "USER_REPLY.md":
                        continue
                    if child.is_dir():
                        continue
                    attachments.append(safe_relpath(child, workspace_root))
                except OSError:
                    continue
        except OSError:
            attachments = []

        if attachments:
            block_lines.append("Attachments:\n- " + "\n- ".join(attachments))

        blocks.append("\n".join(block_lines).strip())

    rendered = "\n\n".join(blocks).strip()
    return rendered, max_seq
