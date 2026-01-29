from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from .frontmatter import parse_markdown_frontmatter
from .lint import lint_ticket_frontmatter
from .models import TicketDoc, TicketFrontmatter

# Accept TICKET-###.md or TICKET-###<suffix>.md (suffix optional), case-insensitive.
_TICKET_NAME_RE = re.compile(r"^TICKET-(\d{3,})(?:[^/]*)\.md$", re.IGNORECASE)


def parse_ticket_index(name: str) -> Optional[int]:
    match = _TICKET_NAME_RE.match(name)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def list_ticket_paths(ticket_dir: Path) -> list[Path]:
    if not ticket_dir.exists() or not ticket_dir.is_dir():
        return []
    tickets: list[tuple[int, Path]] = []
    for path in ticket_dir.iterdir():
        if not path.is_file():
            continue
        idx = parse_ticket_index(path.name)
        if idx is None:
            continue
        tickets.append((idx, path))
    tickets.sort(key=lambda pair: pair[0])
    return [p for _, p in tickets]


def read_ticket(path: Path) -> tuple[Optional[TicketDoc], list[str]]:
    """Read and validate a ticket file.

    Returns (ticket_doc, lint_errors). When lint errors are present, ticket_doc will
    be None.
    """

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, [f"Failed to read ticket: {exc}"]

    data, body = parse_markdown_frontmatter(raw)
    idx = parse_ticket_index(path.name)
    if idx is None:
        return None, [
            "Invalid ticket filename; expected TICKET-<number>[suffix].md (e.g. TICKET-001-foo.md)"
        ]

    frontmatter, errors = lint_ticket_frontmatter(data)
    if errors:
        return None, errors
    assert frontmatter is not None
    return TicketDoc(path=path, index=idx, frontmatter=frontmatter, body=body), []


def read_ticket_frontmatter(
    path: Path,
) -> tuple[Optional[TicketFrontmatter], list[str]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, [f"Failed to read ticket: {exc}"]
    data, _ = parse_markdown_frontmatter(raw)
    frontmatter, errors = lint_ticket_frontmatter(data)
    return frontmatter, errors


def ticket_is_done(path: Path) -> bool:
    frontmatter, errors = read_ticket_frontmatter(path)
    if errors or not frontmatter:
        return False
    return bool(frontmatter.done)


def safe_relpath(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)
