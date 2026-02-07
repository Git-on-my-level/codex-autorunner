from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from ..core.utils import atomic_write
from .files import list_ticket_paths, safe_relpath
from .frontmatter import parse_markdown_frontmatter
from .lint import lint_ticket_frontmatter, parse_ticket_index


@dataclass
class TicketBulkEditResult:
    updated: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


def parse_ticket_range(range_spec: Optional[str]) -> Optional[tuple[int, int]]:
    if range_spec is None:
        return None
    cleaned = str(range_spec).strip()
    if not cleaned:
        return None

    if ":" in cleaned:
        parts = cleaned.split(":")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ValueError("Range must be in the form A:B")
        try:
            start = int(parts[0], 10)
            end = int(parts[1], 10)
        except ValueError as exc:
            raise ValueError("Range must contain integer indices (A:B)") from exc
    else:
        try:
            start = int(cleaned, 10)
        except ValueError as exc:
            raise ValueError("Range must contain integer indices (A:B)") from exc
        end = start

    if start < 1 or end < 1:
        raise ValueError("Range indices must be >= 1")
    if start > end:
        raise ValueError("Range start must be <= end")
    return start, end


def _render_ticket(frontmatter: dict[str, Any], body: str) -> str:
    fm_yaml = yaml.safe_dump(frontmatter, sort_keys=False).rstrip()
    return f"---\n{fm_yaml}\n---{body}"


def _select_ticket_paths(
    ticket_dir: Path, ticket_range: Optional[tuple[int, int]]
) -> list[Path]:
    paths = list_ticket_paths(ticket_dir)
    if ticket_range is None:
        return paths
    start, end = ticket_range
    selected: list[Path] = []
    for path in paths:
        idx = parse_ticket_index(path.name)
        if idx is None:
            continue
        if start <= idx <= end:
            selected.append(path)
    return selected


def _apply_ticket_update(
    path: Path,
    mutate: Callable[[dict[str, Any]], None],
    repo_root: Path,
) -> tuple[bool, bool, list[str]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return False, False, [f"{safe_relpath(path, repo_root)}: {exc}"]

    data, body = parse_markdown_frontmatter(raw)
    frontmatter, errors = lint_ticket_frontmatter(data)
    if errors or frontmatter is None:
        return (
            False,
            False,
            [f"{safe_relpath(path, repo_root)}: {err}" for err in errors],
        )

    updated = dict(data) if isinstance(data, dict) else {}
    mutate(updated)

    _, errors = lint_ticket_frontmatter(updated)
    if errors:
        return (
            False,
            False,
            [f"{safe_relpath(path, repo_root)}: {err}" for err in errors],
        )

    rendered = _render_ticket(updated, body)
    if rendered == raw:
        return True, False, []

    atomic_write(path, rendered)
    return True, True, []


def _bulk_update(
    ticket_dir: Path,
    range_spec: Optional[str],
    mutate: Callable[[dict[str, Any]], None],
    repo_root: Path,
) -> TicketBulkEditResult:
    ticket_range = parse_ticket_range(range_spec)
    paths = _select_ticket_paths(ticket_dir, ticket_range)
    result = TicketBulkEditResult()

    if not paths:
        result.errors.append("No tickets matched the requested range.")
        return result

    for path in paths:
        ok, changed, errors = _apply_ticket_update(path, mutate, repo_root)
        if errors:
            result.errors.extend(errors)
        if not ok:
            result.skipped += 1
            continue
        if changed:
            result.updated += 1
        else:
            result.skipped += 1

    return result


def bulk_set_agent(
    ticket_dir: Path,
    agent: str,
    range_spec: Optional[str],
    *,
    repo_root: Path,
) -> TicketBulkEditResult:
    return _bulk_update(
        ticket_dir,
        range_spec,
        lambda fm: fm.__setitem__("agent", agent),
        repo_root,
    )


def bulk_clear_model_pin(
    ticket_dir: Path,
    range_spec: Optional[str],
    *,
    repo_root: Path,
) -> TicketBulkEditResult:
    def mutate(fm: dict[str, Any]) -> None:
        fm.pop("model", None)
        fm.pop("reasoning", None)

    return _bulk_update(ticket_dir, range_spec, mutate, repo_root)
