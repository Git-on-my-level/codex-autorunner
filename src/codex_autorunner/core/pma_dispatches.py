from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from ..tickets.frontmatter import parse_markdown_frontmatter
from .time_utils import now_iso
from .utils import atomic_write

logger = logging.getLogger(__name__)

PMA_DISPATCHES_DIRNAME = "dispatches"
PMA_DISPATCH_ALLOWED_PRIORITIES = {"info", "warn", "action"}
PMA_DISPATCH_ALLOWED_EXTS = {".md", ".markdown"}


@dataclass(frozen=True)
class PmaDispatch:
    dispatch_id: str
    title: str
    body: str
    priority: str
    links: list[dict[str, str]]
    created_at: str
    resolved_at: Optional[str]
    source_turn_id: Optional[str]
    path: Path


def _dispatches_root(hub_root: Path) -> Path:
    return hub_root / ".codex-autorunner" / "pma" / PMA_DISPATCHES_DIRNAME


def ensure_pma_dispatches_dir(hub_root: Path) -> Path:
    root = _dispatches_root(hub_root)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _normalize_priority(value: Any) -> str:
    if not isinstance(value, str):
        return "info"
    priority = value.strip().lower()
    if priority in PMA_DISPATCH_ALLOWED_PRIORITIES:
        return priority
    return "info"


def _normalize_links(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    links: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        label = item.get("label")
        href = item.get("href")
        if not isinstance(label, str) or not isinstance(href, str):
            continue
        label = label.strip()
        href = href.strip()
        if not label or not href:
            continue
        links.append({"label": label, "href": href})
    return links


def _created_at_from_path(path: Path) -> str:
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return now_iso()
    return datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat()


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def parse_pma_dispatch(path: Path) -> tuple[Optional[PmaDispatch], list[str]]:
    errors: list[str] = []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, [f"Failed to read dispatch: {exc}"]

    data, body = parse_markdown_frontmatter(raw)
    if not isinstance(data, dict):
        data = {}

    raw_title = data.get("title")
    title = raw_title if isinstance(raw_title, str) else ""
    body = body or ""
    priority = _normalize_priority(data.get("priority"))
    links = _normalize_links(data.get("links"))
    created_at = (
        data.get("created_at") if isinstance(data.get("created_at"), str) else None
    )
    resolved_at = (
        data.get("resolved_at") if isinstance(data.get("resolved_at"), str) else None
    )
    source_turn_id = (
        data.get("source_turn_id")
        if isinstance(data.get("source_turn_id"), str)
        else None
    )

    if not created_at:
        created_at = _created_at_from_path(path)

    dispatch_id = path.stem

    dispatch = PmaDispatch(
        dispatch_id=dispatch_id,
        title=title,
        body=body,
        priority=priority,
        links=links,
        created_at=created_at,
        resolved_at=resolved_at,
        source_turn_id=source_turn_id,
        path=path,
    )
    return dispatch, errors


def list_pma_dispatches(
    hub_root: Path,
    *,
    include_resolved: bool = False,
    limit: Optional[int] = None,
) -> list[PmaDispatch]:
    root = ensure_pma_dispatches_dir(hub_root)
    dispatches: list[PmaDispatch] = []
    for child in sorted(root.iterdir(), key=lambda p: p.name):
        if child.suffix.lower() not in PMA_DISPATCH_ALLOWED_EXTS:
            continue
        dispatch, errors = parse_pma_dispatch(child)
        if errors or dispatch is None:
            continue
        if not include_resolved and dispatch.resolved_at:
            continue
        dispatches.append(dispatch)

    dispatches.sort(
        key=lambda d: _parse_iso(d.created_at)
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    if isinstance(limit, int) and limit > 0:
        dispatches = dispatches[:limit]
    return dispatches


def find_pma_dispatch_path(hub_root: Path, dispatch_id: str) -> Optional[Path]:
    if not isinstance(dispatch_id, str) or not dispatch_id:
        return None
    safe_id = dispatch_id.strip()
    if not safe_id or "/" in safe_id or "\\" in safe_id:
        return None
    root = ensure_pma_dispatches_dir(hub_root)
    for ext in PMA_DISPATCH_ALLOWED_EXTS:
        candidate = root / f"{safe_id}{ext}"
        if candidate.exists():
            return candidate
    return None


def resolve_pma_dispatch(path: Path) -> tuple[Optional[PmaDispatch], list[str]]:
    dispatch, errors = parse_pma_dispatch(path)
    if errors or dispatch is None:
        return dispatch, errors

    if dispatch.resolved_at:
        return dispatch, []

    data = {
        "title": dispatch.title,
        "priority": dispatch.priority,
        "links": dispatch.links,
        "created_at": dispatch.created_at,
        "resolved_at": now_iso(),
        "source_turn_id": dispatch.source_turn_id,
    }
    frontmatter = yaml.safe_dump(data, sort_keys=False).strip()
    content = f"---\n{frontmatter}\n---\n\n{dispatch.body.strip()}\n"
    try:
        atomic_write(path, content)
    except OSError as exc:
        return dispatch, [f"Failed to resolve dispatch: {exc}"]

    updated, parse_errors = parse_pma_dispatch(path)
    if parse_errors:
        return updated, parse_errors
    return updated, []


__all__ = [
    "PmaDispatch",
    "PMA_DISPATCHES_DIRNAME",
    "ensure_pma_dispatches_dir",
    "parse_pma_dispatch",
    "list_pma_dispatches",
    "find_pma_dispatch_path",
    "resolve_pma_dispatch",
]
