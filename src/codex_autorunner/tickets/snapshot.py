from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .files import list_ticket_paths, read_ticket_frontmatter, safe_relpath


@dataclass(frozen=True)
class TicketSnapshot:
    rel_path: str
    filename: str
    title: str
    is_done: bool
    label: str


def _ticket_dir(workspace_root: Path) -> Path:
    return workspace_root / ".codex-autorunner" / "tickets"


def _normalize_status_filter(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in {"all", "open", "done"}:
        return "all"
    return normalized


def list_ticket_snapshots(
    workspace_root: Path,
    *,
    status_filter: str = "all",
    paths: Sequence[Path] | None = None,
) -> list[TicketSnapshot]:
    normalized_filter = _normalize_status_filter(status_filter)
    ticket_dir = _ticket_dir(workspace_root)
    snapshots: list[TicketSnapshot] = []
    path_iter = paths if paths is not None else list_ticket_paths(ticket_dir)
    for path in path_iter:
        frontmatter, errors = read_ticket_frontmatter(path)
        is_done = bool(frontmatter and frontmatter.done and not errors)
        if normalized_filter == "open" and is_done:
            continue
        if normalized_filter == "done" and not is_done:
            continue
        title = frontmatter.title.strip() if frontmatter and frontmatter.title else ""
        label = f"{path.name}{' - ' + title if title else ''}"
        rel_path = safe_relpath(path, workspace_root)
        snapshots.append(
            TicketSnapshot(
                rel_path=rel_path,
                filename=path.name,
                title=title,
                is_done=is_done,
                label=label,
            )
        )
    return snapshots


__all__ = [
    "TicketSnapshot",
    "list_ticket_snapshots",
]
