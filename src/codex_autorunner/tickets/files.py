from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Optional

from .frontmatter import parse_markdown_frontmatter
from .lint import lint_ticket_frontmatter, parse_ticket_index
from .models import TicketDoc, TicketFrontmatter


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


def normalize_ticket_dir(repo_root: Path, ticket_dir: Optional[str]) -> Path:
    """Normalize a user-supplied ticket directory and ensure it stays in-tree."""

    base = (repo_root / ".codex-autorunner").resolve(strict=False)
    if not ticket_dir:
        return base / "tickets"

    cleaned = str(ticket_dir).strip()
    if not cleaned:
        return base / "tickets"
    if "\\" in cleaned:
        raise ValueError("Ticket directory may not include backslashes.")

    raw_path = Path(cleaned)
    if raw_path.is_absolute():
        candidate = raw_path.resolve(strict=False)
    else:
        relative = PurePosixPath(cleaned)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Ticket directory must be a relative path.")
        candidate = (repo_root / relative).resolve(strict=False)

    try:
        candidate.relative_to(base)
    except ValueError:
        raise ValueError(
            "Ticket directory must live under .codex-autorunner."
        ) from None
    return candidate
