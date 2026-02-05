from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from codex_autorunner.core.filesystem import copy_path

from .frontmatter import parse_markdown_frontmatter
from .lint import lint_dispatch_frontmatter
from .models import Dispatch, DispatchRecord

_lifecycle_emitter: Optional[Callable[[str, str, str, Dict[str, Any]], None]] = None


def set_lifecycle_emitter(
    emitter: Optional[Callable[[str, str, str, Dict[str, Any]], None]],
) -> None:
    global _lifecycle_emitter
    _lifecycle_emitter = emitter


def _emit_lifecycle(
    event_type: str, repo_id: str, run_id: str, data: Dict[str, Any]
) -> None:
    if _lifecycle_emitter:
        try:
            _lifecycle_emitter(event_type, repo_id, run_id, data)
        except Exception:
            pass


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


def create_turn_summary(
    paths: OutboxPaths,
    *,
    next_seq: int,
    agent_output: str,
    ticket_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    turn_number: Optional[int] = None,
    diff_stats: Optional[dict] = None,
) -> tuple[Optional[DispatchRecord], list[str]]:
    """Create a turn summary dispatch record for the agent's final output.

    This creates a synthetic dispatch with mode="turn_summary" to show
    the agent's final turn output in the dispatch history panel.

    Args:
        paths: Outbox paths for the run
        next_seq: Sequence number for this dispatch
        agent_output: The agent's output text
        ticket_id: Optional ticket ID for context
        agent_id: Optional agent ID (e.g., "codex", "opencode")
        turn_number: Optional turn number
        diff_stats: Optional dict with insertions/deletions/files_changed.
            Deprecated: diff stats are now stored as FlowStore DIFF_UPDATED events.

    Returns (DispatchRecord, []) on success.
    Returns (None, errors) on failure.
    """

    if not agent_output or not agent_output.strip():
        return None, []

    extra: dict = {}
    if ticket_id:
        extra["ticket_id"] = ticket_id
    if agent_id:
        extra["agent_id"] = agent_id
    if turn_number is not None:
        extra["turn_number"] = turn_number
    # NOTE: diff_stats is intentionally not persisted into DISPATCH.md frontmatter.
    # It is stored as structured FlowStore DIFF_UPDATED events instead.
    extra["is_turn_summary"] = True

    dispatch = Dispatch(
        mode="turn_summary",
        body=agent_output.strip(),
        title=None,
        extra=extra,
    )

    dest = paths.dispatch_history_dir / f"{next_seq:04d}"
    try:
        dest.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        return None, [f"Failed to create turn summary dir: {exc}"]

    # Write a synthetic DISPATCH.md for consistency
    msg_dest = dest / "DISPATCH.md"
    try:
        # Write minimal frontmatter + body
        content = f"---\nmode: turn_summary\n---\n\n{agent_output.strip()}\n"
        msg_dest.write_text(content, encoding="utf-8")
    except OSError as exc:
        return None, [f"Failed to write turn summary: {exc}"]

    return (
        DispatchRecord(
            seq=next_seq,
            dispatch=dispatch,
            archived_dir=dest,
            archived_files=(msg_dest,),
        ),
        [],
    )


def archive_dispatch(
    paths: OutboxPaths,
    *,
    next_seq: int,
    ticket_id: Optional[str] = None,
    repo_id: str = "",
    run_id: str = "",
) -> tuple[Optional[DispatchRecord], list[str]]:
    """Archive current dispatch and attachments to dispatch history.

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

    # Add ticket_id to extra if provided
    if ticket_id and dispatch is not None:
        extra = dict(dispatch.extra)
        extra["ticket_id"] = ticket_id
        dispatch = Dispatch(
            mode=dispatch.mode,
            body=dispatch.body,
            title=dispatch.title,
            extra=extra,
        )

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
        copy_path(paths.dispatch_path, msg_dest)
        archived.append(msg_dest)

        # Archive all attachments.
        for item in items:
            item_dest = dest / item.name
            copy_path(item, item_dest)
            archived.append(item_dest)

    except OSError as exc:
        return None, [f"Failed to archive dispatch: {exc}"]

    # Cleanup (best-effort).
    try:
        paths.dispatch_path.unlink()
    except OSError:
        pass
    _delete_dispatch_items(items)

    # Emit lifecycle event for dispatch creation
    if run_id:
        _emit_lifecycle(
            "dispatch_created",
            repo_id,
            run_id,
            {
                "seq": next_seq,
                "mode": dispatch.mode,
                "title": dispatch.title,
                "ticket_id": ticket_id,
            },
        )

    return (
        DispatchRecord(
            seq=next_seq,
            dispatch=dispatch,
            archived_dir=dest,
            archived_files=tuple(archived),
        ),
        [],
    )
