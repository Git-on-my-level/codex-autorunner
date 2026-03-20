from __future__ import annotations

from pathlib import Path

from ..tickets.frontmatter import parse_markdown_frontmatter, sanitize_ticket_id


def ticket_stable_id(path: Path) -> str | None:
    """Return explicit frontmatter ticket_id when present."""
    if not path.exists():
        return None
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None
    data, _ = parse_markdown_frontmatter(content)
    return sanitize_ticket_id(data.get("ticket_id"))


def ticket_instance_token(path: Path) -> str:
    """Return a stable ticket identity token.

    Uses explicit frontmatter `ticket_id` so normal saves (which use atomic_write)
    do not churn identity.
    """
    ticket_id = ticket_stable_id(path)
    if ticket_id:
        return ticket_id
    if not path.exists():
        return "missing-ticket"
    return "invalid-ticket-id"


def ticket_chat_scope(index: int, path: Path) -> str:
    return f"ticket:{index}:{ticket_instance_token(path)}"


def ticket_state_key(index: int, path: Path) -> str:
    return f"ticket_{index:03d}_{ticket_instance_token(path)}"
